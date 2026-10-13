from django.contrib.admin.apps import AdminConfig


class ReviewFlowAdminConfig(AdminConfig):
    """Installs config.admin.ReviewFlowAdminSite as the default admin.site
    (spec 16)."""

    default_site = "config.admin.ReviewFlowAdminSite"
