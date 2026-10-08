"""WhatsAppProvider interface (WhatsApp-Architecture.md §Provider Adapter).

Campaign code never imports a provider: it calls whatsapp.services only. Error
messages never carry a phone number, token, body or provider response.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from core.exceptions import ReviewFlowError


class ProviderTransientError(ReviewFlowError):
    """Network failure, timeout, 429 or 5xx: retry with backoff."""


class ProviderPermanentError(ReviewFlowError):
    """4xx validation/permission failure: never retried."""


@dataclass(frozen=True)
class ProviderSendResult:
    provider_message_id: str


@dataclass(frozen=True)
class StatusUpdate:
    provider_message_id: str
    status: str  # SENT, DELIVERED, READ or FAILED
    occurred_at: datetime | None
    error_code: int | None = None


@dataclass(frozen=True)
class InboundMessage:
    phone_number_id: str
    from_phone: str  # digits as Meta sends them, without a leading "+"
    text: str | None


@dataclass(frozen=True)
class TemplateStatus:
    status: str  # PENDING, APPROVED or REJECTED


class WhatsAppProvider(ABC):
    @abstractmethod
    def send(self, account, to: str, template, variables: dict) -> ProviderSendResult: ...

    @abstractmethod
    def parse_status_webhook(self, payload: dict) -> list[StatusUpdate]: ...

    @abstractmethod
    def parse_inbound_webhook(self, payload: dict) -> list[InboundMessage]: ...

    @abstractmethod
    def submit_template(self, account, template) -> str:
        """Returns provider_template_id."""

    @abstractmethod
    def find_template_id(self, account, template) -> str | None:
        """The provider id of a template ReviewFlow already submitted under its
        derived name and language, or None. Used to recover a submission the
        provider accepted but ReviewFlow never recorded."""

    @abstractmethod
    def fetch_template_status(self, account, provider_template_id: str) -> TemplateStatus: ...

    @abstractmethod
    def fetch_quality_rating(self, account) -> str: ...

    @abstractmethod
    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool: ...

    @abstractmethod
    def register_number(self, merchant, number): ...
