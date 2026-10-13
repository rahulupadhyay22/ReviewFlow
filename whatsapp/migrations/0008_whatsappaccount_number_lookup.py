from django.db import migrations

from core.rls import rls_select_by_phone_number_id


class Migration(migrations.Migration):
    """Spec 08-whatsapp-embedded-signup ES-3 (LOCKED DECISION CHANGE, approved
    2026-10-09): the seventh, SELECT-only policy on whatsapp_whatsappaccount,
    for the post-signature inbound STOP lookup of an own number. The six
    Change 1 policies (0002) are unchanged.

    Rollback WARNING: reversing this drops the policy, so the lookup then
    sees nothing (fail closed) and a STOP to an own number is silently not
    honoured. Roll the code back together with this migration."""

    dependencies = [("whatsapp", "0007_messagetemplate_unique_per_waba")]

    operations = [rls_select_by_phone_number_id("whatsapp_whatsappaccount")]
