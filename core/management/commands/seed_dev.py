"""Local dev seed data (Development-Setup.md §"Seed Data"). Extended in
Phase 07 (Plan/Subscription) and Phase 08 (SHARED_POOL WhatsAppAccount)."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts import services
from accounts.models import User
from core.tenancy import tenant_atomic, tenant_context

SEED_OWNER_EMAIL = "owner@seed.reviewflow.local"
SEED_MANAGER_EMAIL = "manager@seed.reviewflow.local"

# DEVELOPMENT FIXTURES ONLY (spec 07 Decision 10). These plan names, the zero
# price and the placeholder quotas exist so a local database has something to
# show. They are NOT pricing and NOT quota decisions, and nothing here may be
# copied into production data: production plans are Razorpay plans entered in
# Django Admin with their provider_plan_id.
DEV_PLANS = (("Starter", 100), ("Growth", 500), ("Pro", 2000), ("Business", 10000))
DEV_SUBSCRIPTION_PLAN = "Growth"
DEV_PERIOD = timedelta(days=365)


def seed_billing(merchant) -> bool:
    """The Phase 07 billing fixtures: four dev plans, and an ACTIVE
    subscription on one of them with its UsageRecord for the seed merchant.
    Idempotent on its own, so a database seeded before Phase 07 gains them on
    the next run. Returns whether anything was created.

    This is the only code allowed to create an ACTIVE subscription without a
    paid-invoice proof (spec 07 "Rules for implementation"): it has no
    provider, so the row has no payment_provider_ref and no PaymentAttempt.
    Local imports keep this command's import order acyclic."""
    from billing import services as billing_services
    from billing.models import Plan, Subscription

    created = False
    plans = {}
    with transaction.atomic():  # all four dev plans or none, never a partial set
        for name, quota in DEV_PLANS:
            plan = Plan.objects.filter(name=name, is_active=True).first()
            if plan is None:
                plan = Plan.objects.create(name=name, monthly_price=0, quota_requests=quota, provider_plan_id=None)
                created = True
            plans[name] = plan

    with tenant_context(merchant.id), tenant_atomic():
        if not Subscription.objects.exists():
            now = timezone.now()
            subscription = Subscription.objects.create(
                merchant=merchant,
                plan=plans[DEV_SUBSCRIPTION_PLAN],
                status=Subscription.Status.ACTIVE,
                current_period_start=now,
                current_period_end=now + DEV_PERIOD,
            )
            billing_services.reset_usage_period(subscription)
            created = True
    return created


# DEVELOPMENT FIXTURES ONLY: obviously fake Meta ids. Production's shared
# sender is configured with `manage.py configure_shared_pool`.
DEV_SHARED_PHONE_NUMBER_ID = "DEV-SHARED-PHONE-ID"
DEV_SHARED_BUSINESS_ACCOUNT_ID = "DEV-SHARED-WABA-ID"


def seed_whatsapp(merchant) -> bool:
    """The Phase 08 fixtures: the platform SHARED_POOL WhatsAppAccount, and
    both seed locations mapped to it. Idempotent on its own, so a database
    seeded before Phase 08 gains them on the next run. Returns whether
    anything was created. Local imports keep this command's import order
    acyclic."""
    from locations.models import Location
    from whatsapp import services as whatsapp_services
    from whatsapp.models import WhatsAppAccount, WhatsAppLocationMapping

    created = not WhatsAppAccount.objects.shared_pool().exists()
    # No merchant context here: the shared row is written through the audited
    # platform path (core.tenancy.platform_write_atomic) only.
    account = whatsapp_services.upsert_shared_pool_account(
        phone_number_id=DEV_SHARED_PHONE_NUMBER_ID,
        business_account_id=DEV_SHARED_BUSINESS_ACCOUNT_ID,
        status=WhatsAppAccount.Status.ACTIVE,
    )
    with tenant_context(merchant.id), tenant_atomic():
        for location in Location.objects.order_by("created_at"):
            if not WhatsAppLocationMapping.objects.filter(location=location).exists():
                whatsapp_services.set_location_sender(location=location, account=account, actor=None)
                created = True
    return created


class Command(BaseCommand):
    help = "Creates a local test Merchant with 2 Locations and a MANAGER assigned to one."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=None)

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_dev only runs with DJANGO_DEBUG=True.")

        existing_owner = User.objects.filter(email=SEED_OWNER_EMAIL).first()
        if existing_owner is not None:
            # The billing step is independent of the rest of the seed, so a
            # database seeded before Phase 07 gains it here.
            membership = services.resolve_login_membership(existing_owner)
            added = seed_billing(membership.merchant) if membership is not None else False
            added_whatsapp = seed_whatsapp(membership.merchant) if membership is not None else False
            self.stdout.write("already seeded")
            if added:
                self.stdout.write(self.style.SUCCESS("Billing dev fixtures added (development values only)."))
            if added_whatsapp:
                self.stdout.write(self.style.SUCCESS("WhatsApp dev fixtures added (development values only)."))
            return

        password = options["password"] or secrets.token_urlsafe(16)

        owner = services.create_merchant_with_owner(
            name="Seed Cafe",
            timezone="Asia/Kolkata",
            owner_email=SEED_OWNER_EMAIL,
            owner_password=password,
        )
        merchant = owner.merchant

        with tenant_context(merchant.id):
            # Local import: locations app depends on accounts, keeping this
            # command's own import order acyclic with accounts.services.
            from locations import services as location_services

            first = location_services.create_location(name="Seed Cafe — Indiranagar")
            location_services.create_location(name="Seed Cafe — Koramangala")

            manager, invite_token = services.invite_team_member(
                actor=owner, email=SEED_MANAGER_EMAIL, role="MANAGER"
            )

        # accept_invite runs its own read (user_lookup_atomic) then write
        # (tenant_context); it must not be called from inside another
        # tenant context.
        services.accept_invite(token=invite_token, password=password)

        with tenant_context(merchant.id):
            manager = services.set_team_member_locations(
                actor=owner, member_id=manager.id, location_ids=[first.id]
            )

        seed_billing(merchant)
        seed_whatsapp(merchant)

        self.stdout.write(self.style.SUCCESS("Seed data created."))
        self.stdout.write(f"Owner:   {SEED_OWNER_EMAIL}")
        self.stdout.write(f"Manager: {SEED_MANAGER_EMAIL}")
        self.stdout.write("Billing: 4 dev plans + an ACTIVE Growth subscription (development values only).")
        self.stdout.write("WhatsApp: the SHARED_POOL dev sender, both locations mapped (development values only).")
        self.stdout.write(f"Password: {password}")
