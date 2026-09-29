"""Provider -> adapter class registry (Integration-Architecture.md
§"The Adapter Contract": "resolves the adapter from the receiving
Integration row ... rather than from the event's source string alone").

Phase 06 registers webhook/csv/api/shopify here (shopify added by the
06-shopify-app workstream). woocommerce/petpooja/gofrugal/zapier/make are
Phase 17 scope and stay unregistered. Tests may still register a FakeAdapter
via monkeypatch.setitem(ADAPTERS, ...).
"""
from core.exceptions import ReviewFlowError
from integrations.api.adapter import ApiAdapter
from integrations.core.adapters import BaseAdapter
from integrations.csv_import.adapter import CsvImportAdapter
from integrations.shopify.adapter import ShopifyAdapter
from integrations.webhook.adapter import GenericWebhookAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "webhook": GenericWebhookAdapter,
    "csv": CsvImportAdapter,
    "api": ApiAdapter,
    "shopify": ShopifyAdapter,
}


class AdapterNotFound(ReviewFlowError):
    """No adapter is registered for the integration's provider. No HTTP
    mapping: events.services maps it to the ADAPTER_NOT_FOUND safe error."""


def is_registered(provider: str) -> bool:
    return provider in ADAPTERS


def get_adapter_class(provider: str) -> type[BaseAdapter]:
    """Resolves the adapter class for a provider without an Integration
    instance -- used by connect_integration(), which validates/issues
    credentials before any row exists."""
    try:
        return ADAPTERS[provider]
    except KeyError:
        raise AdapterNotFound(f"No adapter registered for provider {provider!r}.") from None


def get_adapter(integration) -> BaseAdapter:
    try:
        adapter_cls = ADAPTERS[integration.provider]
    except KeyError:
        raise AdapterNotFound(
            f"No adapter registered for provider {integration.provider!r}."
        ) from None
    return adapter_cls(integration)
