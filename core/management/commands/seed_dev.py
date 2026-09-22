"""Local dev seed data (Development-Setup.md §"Seed Data"). Extended in
Phase 07 (Plan/Subscription) and Phase 08 (SHARED_POOL WhatsAppAccount)."""
import secrets

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts import services
from accounts.models import User
from core.tenancy import tenant_context

SEED_OWNER_EMAIL = "owner@seed.reviewflow.local"
SEED_MANAGER_EMAIL = "manager@seed.reviewflow.local"


class Command(BaseCommand):
    help = "Creates a local test Merchant with 2 Locations and a MANAGER assigned to one."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=None)

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_dev only runs with DJANGO_DEBUG=True.")

        if User.objects.filter(email=SEED_OWNER_EMAIL).exists():
            self.stdout.write("already seeded")
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

        self.stdout.write(self.style.SUCCESS("Seed data created."))
        self.stdout.write(f"Owner:   {SEED_OWNER_EMAIL}")
        self.stdout.write(f"Manager: {SEED_MANAGER_EMAIL}")
        self.stdout.write(f"Password: {password}")
