"""BaseAdapter: the contract every provider implementation follows
(Integration-Architecture.md §"The Adapter Contract"; SAD.md §4).

Phase 04 defined verify()/parse()/normalize() only. Phase 06 (spec Decision
6) adds hooks the receivers and connect/PATCH endpoints need, all additive:
the verify/parse/normalize contract itself is unchanged. Every default is
inert/fail-closed, so a provider that doesn't override a hook behaves
exactly as before.
"""
from abc import ABC, abstractmethod

from integrations.core.events import SaleCreated


class BaseAdapter(ABC):
    def __init__(self, integration):
        self.integration = integration

    @abstractmethod
    def verify(self, request) -> bool:
        """Signature/secret check. Must fail closed (Authentication.md §3)."""
        raise NotImplementedError

    @abstractmethod
    def parse(self, payload: dict) -> dict:
        """Provider-specific parsing/validation of the stored payload.
        Raises integrations.core.schemas.PayloadValidationError."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, parsed: dict) -> SaleCreated:
        """parsed -> the common SaleCreated shape."""
        raise NotImplementedError

    @abstractmethod
    def get_external_event_id(self, request, payload: dict) -> str:
        """The receipt-time idempotency key (spec Decision 6/7), computed
        before parse() runs in the task. Raises
        integrations.core.schemas.PayloadValidationError on failure."""
        raise NotImplementedError

    @classmethod
    def validate_connection(cls, credentials: dict | None, config_json: dict | None) -> None:
        """Provider-specific connect/PATCH validation (spec Decision 6).
        Raises django.core.exceptions.ValidationError. The default is a
        no-op -- most providers accept any well-formed JSON object."""

    @classmethod
    def issue_credentials(cls) -> dict | None:
        """Server-generated credentials to store instead of any
        client-supplied ones (spec Decision 6). The default is None."""
        return None

    def is_sale_event(self, request) -> bool:
        """Whether this delivery represents a sale to ingest (spec
        Decision 6). The default is True: every non-Shopify source
        delivers only sale events."""
        return True

    def is_uninstall_event(self, request) -> bool:
        """Whether this delivery is a provider app-uninstall notification
        (spec 06 Decision 3). The default is False. This only *routes* --
        it reads an unsigned header and must never by itself authorize a
        destructive action; see uninstall_payload_matches()."""
        return False

    def uninstall_payload_matches(self, payload: dict) -> bool:
        """The Phase 06 current-topic discriminator (spec 06 Decision 3,
        step 2b) that gates the destructive uninstall. It is NOT a
        cryptographic proof and does NOT authenticate any header -- it only
        separates the documented payload shapes of the topics an adapter
        currently subscribes to. The base default is fail-closed: an
        adapter that never handles an uninstall topic can never authorize
        one."""
        return False
