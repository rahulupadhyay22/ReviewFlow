"""Two-thread race harness for the Subscription row lock (plain helper, not a
conftest). The first thread takes the real lock inside
`services._lock_subscription` (the wrapper calls the real function first) and
holds it; the second thread is started only then; the main thread polls
pg_stat_activity until the SECOND thread's own backend is waiting on a lock,
with a bounded deadline, and only then releases the first. No fixed sleeps: the
observed lock wait is part of the test, not an assumption. Needs
django_db(transaction=True) so each thread's work really commits."""
import threading
import time

from django.db import connection

from billing import services

DEADLINE = 30  # seconds, a bound on every wait


def waiting_on_a_lock(pid) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND wait_event_type = 'Lock'", [pid]
        )
        return cursor.fetchone()[0] == 1


def race(monkeypatch, *, first, second):
    """Run `first` and `second` (callables of no arguments) in two threads: the
    first holds the Subscription row lock, the second is observed blocked on
    it, then the first is released. Returns the second thread's backend pid;
    raises on any failure in either thread."""
    locked, release, second_pid_known = threading.Event(), threading.Event(), threading.Event()
    real_lock = services._lock_subscription
    errors, second_pid = [], []

    def lock_then_hold(subscription_id):
        subscription = real_lock(subscription_id)  # the real row lock is taken first
        if threading.current_thread().name == "first" and not locked.is_set():
            locked.set()
            assert release.wait(DEADLINE), "the first thread was never released"
        return subscription

    monkeypatch.setattr(services, "_lock_subscription", lock_then_hold)

    def run(operation, name):
        try:
            if name == "second":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    second_pid.append(cursor.fetchone()[0])
                second_pid_known.set()
            operation()
        except Exception as exc:  # captured and asserted below
            errors.append((name, exc))
        finally:
            connection.close()

    first_thread = threading.Thread(target=run, args=(first, "first"), name="first")
    second_thread = threading.Thread(target=run, args=(second, "second"), name="second")
    first_thread.start()
    try:
        assert locked.wait(DEADLINE), "the first thread never took the row lock"
        second_thread.start()
        assert second_pid_known.wait(DEADLINE), "the second thread never started"
        deadline = time.monotonic() + DEADLINE
        while not waiting_on_a_lock(second_pid[0]):
            assert time.monotonic() < deadline, "the second thread was never observed waiting on the row lock"
            time.sleep(0.01)  # a poll interval, not a synchronization delay
    finally:
        release.set()
        first_thread.join(DEADLINE)
        if second_thread.ident is not None:
            second_thread.join(DEADLINE)
    assert not errors, errors
    return second_pid[0]
