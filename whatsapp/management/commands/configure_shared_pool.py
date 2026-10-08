"""Configure the platform's single SHARED_POOL sender (spec 08 Change 1).

The only supported way to write the shared row in production, through
whatsapp.services.upsert_shared_pool_account (the audited platform path).
Idempotent. Prints no secret: the Meta access token is an environment
variable, never an argument."""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.exceptions import TenantContextError
from whatsapp import services
from whatsapp.models import WhatsAppAccount


class Command(BaseCommand):
    help = "Create or update the platform's SHARED_POOL WhatsApp sender."

    def add_arguments(self, parser):
        parser.add_argument("--phone-number-id", required=True)
        parser.add_argument("--business-account-id", required=True)
        parser.add_argument(
            "--status", choices=WhatsAppAccount.Status.values, default=WhatsAppAccount.Status.ACTIVE
        )

    def handle(self, *args, **options):
        try:
            account = services.upsert_shared_pool_account(
                phone_number_id=options["phone_number_id"],
                business_account_id=options["business_account_id"],
                status=options["status"],
            )
        except (TenantContextError, ValidationError) as exc:  # unexpected errors surface
            raise CommandError(f"Not configured: {type(exc).__name__}.") from None
        self.stdout.write(self.style.SUCCESS(f"Shared pool sender is {account.status}."))
