"""Celery queue routing (SAD.md §5: queues events, whatsapp, google_sync,
default). The four queues share one exchange and routing key ("default"),
because CELERY_TASK_QUEUES declares plain Queue(name)s. That is harmless for
how this project publishes: a task with a declared queue and no explicit
exchange/routing_key is sent to the nameless exchange with routing key = the
queue name (celery/app/amqp.py, "convert to anon-exchange"), so it reaches
exactly that queue. These tests pin that behaviour.

Every publish goes to an in-memory kombu broker that the fixture asserts
before anything is sent: no Redis, no running worker, nothing executes."""
import pytest
from kombu import Connection

from config import celery_app
from events import tasks as event_tasks
from whatsapp import tasks as whatsapp_tasks

QUEUES = ("events", "whatsapp", "google_sync", "default")


def _memory_connection():
    conn = Connection("memory://")
    assert type(conn.transport).__module__ == "kombu.transport.memory"
    return conn


@pytest.fixture
def broker(monkeypatch):
    """An in-memory broker with the project's queues declared and empty, and
    eager mode off so apply_async really publishes (to memory only)."""
    conn = _memory_connection()
    channel = conn.default_channel
    for queue in celery_app.amqp.queues.values():
        queue.bind(channel).declare()
    for name in QUEUES:
        channel.queue_purge(name)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", False)
    yield conn, channel
    conn.release()


def delivered(channel, publish):
    before = {name: channel._size(name) for name in QUEUES}
    publish()
    return {name: channel._size(name) - before[name] for name in QUEUES if channel._size(name) != before[name]}


@pytest.mark.parametrize(
    ("task", "queue"),
    [
        (whatsapp_tasks.fan_out_inbound_opt_out, "whatsapp"),
        (whatsapp_tasks.process_inbound_opt_out, "whatsapp"),
        (whatsapp_tasks.submit_template, "whatsapp"),
        (whatsapp_tasks.activate_own_number, "default"),
        (event_tasks.process_integration_event, "events"),
    ],
    ids=lambda value: getattr(value, "name", value),
)
def test_a_task_reaches_only_its_declared_queue(broker, task, queue):
    conn, channel = broker

    # ignore_result: no result-backend round trip, so nothing touches Redis.
    landed = delivered(channel, lambda: task.apply_async(args=("x", "y"), connection=conn, ignore_result=True))

    assert task.queue == queue
    assert landed == {queue: 1}


def test_the_webhook_publish_helper_puts_one_fan_out_on_the_whatsapp_queue_only(broker, monkeypatch):
    _, channel = broker
    # The helper opens its own bounded broker connection; give it a memory one.
    monkeypatch.setattr(whatsapp_tasks.celery_app, "connection_for_write", lambda **_: _memory_connection())

    landed = delivered(channel, lambda: whatsapp_tasks.publish_opt_out_fan_out("109999999999001", "+919999990001"))

    assert landed == {"whatsapp": 1}


def test_every_project_task_declares_a_queue_the_worker_consumes():
    celery_app.loader.import_default_modules()
    # Celery's built-ins and tasks defined inside test modules (e.g. core's
    # tenancy tests register one in the same process) are not project tasks.
    project_tasks = {
        name: task
        for name, task in celery_app.tasks.items()
        if not name.startswith("celery.") and ".tests." not in task.__module__
    }

    assert project_tasks
    for name, task in project_tasks.items():
        # A task with no explicit queue goes to task_default_queue.
        assert (getattr(task, "queue", None) or celery_app.conf.task_default_queue) in QUEUES, name
