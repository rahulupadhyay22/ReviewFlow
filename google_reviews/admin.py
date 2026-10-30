"""Django Admin for google_reviews (spec 09-google-business-profile "Admin").

No model is registered. GoogleConnection, GoogleOAuthState, GoogleLocation and
GoogleReview are tenant-scoped, and TenantScopedManager needs a tenant context,
so cross-tenant admin waits for the audited Phase 16 path (same precedent as
whatsapp/admin.py, accounts/admin.py and billing/admin.py). The token columns
are never shown in any admin.
"""
