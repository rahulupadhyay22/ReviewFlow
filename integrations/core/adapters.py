"""BaseAdapter: the contract every provider implementation follows
(Integration-Architecture.md §"The Adapter Contract"; SAD.md §4).

Phase 04 defines the contract only -- no concrete adapter ships until
Phase 06 (Shopify/Generic Webhook/CSV) and Phase 17 (the phased
providers). parse() takes the stored, JSON-decoded payload (spec
Decision 2), not raw bytes: the receiver decodes the body once, at
receipt, and IntegrationEvent.payload stores the result.
"""
from abc import ABC, abstractmethod

from integrations.core.events import SaleCreated


class BaseAdapter(ABC):
    def __init__(self, integration):
        self.integration = integration

    @abstractmethod
    def verify(self, request) -> bool:
        """Signature/secret check. Must fail closed (Authentication.md §3).
        Unused in Phase 04: no webhook receiver exists yet."""
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
