"""Provider lookup: a dict, not a registry framework (one V1 provider)."""
from whatsapp.models import WhatsAppAccount
from whatsapp.providers.base import WhatsAppProvider
from whatsapp.providers.meta_cloud import MetaCloudProvider

_PROVIDERS = {WhatsAppAccount.Provider.META_CLOUD: MetaCloudProvider}


def get_provider_by_name(provider: str) -> WhatsAppProvider:
    """For work that has no account yet, e.g. verifying a webhook before any
    database access."""
    return _PROVIDERS[provider]()


def get_provider(account) -> WhatsAppProvider:
    return get_provider_by_name(account.provider)
