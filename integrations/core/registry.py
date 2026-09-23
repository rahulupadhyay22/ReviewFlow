"""Provider -> adapter class registry (Integration-Architecture.md
§"The Adapter Contract": "resolves the adapter from the receiving
Integration row ... rather than from the event's source string alone").

Empty in Phase 04 -- no concrete adapter is registered here. Phase 06/17
register their adapters under the provider identifier. Tests register a
FakeAdapter via monkeypatch.setitem(ADAPTERS, ...).
"""
from core.exceptions import ReviewFlowError
from integrations.core.adapters import BaseAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {}


class AdapterNotFound(ReviewFlowError):
    """No adapter is registered for the integration's provider. No HTTP
    mapping: events.services maps it to the ADAPTER_NOT_FOUND safe error."""


def is_registered(provider: str) -> bool:
    return provider in ADAPTERS


def get_adapter(integration) -> BaseAdapter:
    try:
        adapter_cls = ADAPTERS[integration.provider]
    except KeyError:
        raise AdapterNotFound(
            f"No adapter registered for provider {integration.provider!r}."
        ) from None
    return adapter_cls(integration)
