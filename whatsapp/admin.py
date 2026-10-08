"""Django Admin for whatsapp (spec 08 "Admin", amended at plan time).

No model is registered. WhatsAppAccount, WhatsAppLocationMapping and
MessageTemplate are tenant-scoped, and TenantScopedManager needs a tenant
context, so cross-tenant admin waits for the audited Phase 16 path (same
precedent as accounts/admin.py and billing/admin.py). The shared sender row is
written only by the configure_shared_pool command and seed_dev; template
status stays visible through GET /whatsapp/templates.
"""
