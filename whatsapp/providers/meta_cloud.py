"""The only module that talks to Meta (WhatsApp Cloud API / Graph API).

No business logic, no database access; called only from whatsapp/services.py.
Stdlib urllib with Bearer auth and an explicit timeout, the same seam style as
billing/razorpay.py. Exception text never contains a response body, phone
number or token; only the class and HTTP status are logged.

Facts verified against Meta's docs (2026-10-07) are recorded in
docs/05-integrations/WhatsApp-Meta.md.
"""
import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.crypto import decrypt
from integrations.core.schemas import verify_hmac_sha256
from whatsapp.exceptions import WhatsAppNotConfigured
from whatsapp.providers.base import (
    InboundMessage,
    ProviderCredential,
    ProviderPermanentError,
    ProviderSendResult,
    ProviderTransientError,
    StatusUpdate,
    TemplateStatus,
    WhatsAppProvider,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10  # seconds
_FIND_PAGE_SIZE = 100
_FIND_MAX_PAGES = 50
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# Template category for a review request. Meta classes feedback on a specific
# order as UTILITY (template-categorization); see WhatsApp-Meta.md.
_TEMPLATE_CATEGORY = "UTILITY"
_STATUS_MAP = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}
_REJECTED_STATES = {"REJECTED", "DISABLED", "DELETED", "ARCHIVED", "PENDING_DELETION"}


def _quote(value) -> str:
    """A provider id as one URL path segment: nothing in it can add a segment,
    a query string or a fragment."""
    return urllib.parse.quote(str(value), safe="")


def provider_template_name(template) -> str:
    """The Meta-side name: derived, never the merchant's free text. Meta
    template names are lowercase alphanumerics and underscores, and every
    shared-pool merchant submits to the same WABA."""
    return f"rf_{template.pk.hex}"


def named_examples(body: str) -> list[dict]:
    """Example values Meta requires for each named placeholder, in order."""
    seen, out = set(), []
    for name in _PLACEHOLDER.findall(body):
        if name not in seen:
            seen.add(name)
            out.append({"param_name": name, "example": f"example_{name}"})
    return out


def _token_for(account) -> str:
    """The Meta token to call with for this account. A SHARED_POOL account
    uses the platform env token, unchanged. An OWN_NUMBER account uses its
    merchant's decrypted business token (spec 08-whatsapp-embedded-signup
    ES-1). Missing, undecryptable or an unset FERNET_KEY all fail closed as
    WhatsAppNotConfigured: there is never a plaintext fallback, and nothing in
    the error names the token or the key."""
    if account.sender_type == account.SenderType.SHARED_POOL:
        token = settings.META_SHARED_POOL_ACCESS_TOKEN
    else:
        ciphertext = account.access_token_enc
        if not ciphertext:
            raise WhatsAppNotConfigured()
        try:
            token = decrypt(ciphertext)
        except (InvalidToken, ImproperlyConfigured):
            raise WhatsAppNotConfigured() from None
    if not token:
        raise WhatsAppNotConfigured()
    return token


def _request(method: str, path: str, body: dict | None = None, *, operation: str, token: str) -> dict:
    if not token:
        raise WhatsAppNotConfigured()
    base = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is a URLError: it must be caught first.
        status = exc.code
        try:
            exc.read()
        except (OSError, http.client.HTTPException):
            pass
        logger.warning("Meta %s answered %s", operation, status)
        if status == 429 or status >= 500:
            raise ProviderTransientError("The WhatsApp provider is unavailable.") from None
        raise ProviderPermanentError("The WhatsApp provider refused the request.") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        logger.warning("Meta %s failed with %s", operation, type(exc).__name__)
        raise ProviderTransientError("The WhatsApp provider is unavailable.") from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ProviderTransientError("The WhatsApp provider returned an unreadable response.") from None
    if not isinstance(parsed, dict):
        raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
    return parsed


def _values(payload: dict):
    """Every webhook `value` object: entry[].changes[].value. A signed body is
    still external input: any level of the wrong JSON type is skipped, never
    dereferenced."""
    entries = payload.get("entry")
    for entry in entries if isinstance(entries, list) else []:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        for change in changes if isinstance(changes, list) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                yield value


def _id_text(value) -> str | None:
    """A Meta id or phone field as text: a non-empty string, or an int as
    Meta's examples sometimes show. Anything else (bool, object, list, empty)
    is None, so it can never be coerced into a lookup key or a phone number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) and value else None


class MetaCloudProvider(WhatsAppProvider):
    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        """X-Hub-Signature-256: `sha256=<hex>`, HMAC-SHA256 of the raw body
        keyed with the app secret. Fails closed, including when the secret is
        unset."""
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        return verify_hmac_sha256(settings.META_APP_SECRET, raw_body, signature_header[len("sha256="):])

    def parse_inbound_webhook(self, payload: dict) -> list[InboundMessage]:
        """Malformed items are skipped one by one, so a valid message in the
        same delivery is still returned. `text` is a string only for a "text"
        message whose `text` object carries a string `body`; otherwise None."""
        out = []
        for value in _values(payload):
            metadata = value.get("metadata")
            phone_number_id = _id_text(metadata.get("phone_number_id")) if isinstance(metadata, dict) else None
            messages = value.get("messages")
            if phone_number_id is None or not isinstance(messages, list):
                continue
            for message in messages:
                sender = _id_text(message.get("from")) if isinstance(message, dict) else None
                if sender is None:
                    continue
                text = None
                if message.get("type") == "text":
                    text_obj = message.get("text")
                    body = text_obj.get("body") if isinstance(text_obj, dict) else None
                    text = body if isinstance(body, str) else None
                out.append(InboundMessage(phone_number_id, sender, text))
        return out

    def parse_status_webhook(self, payload: dict) -> list[StatusUpdate]:
        """Malformed items are skipped one by one, as in parse_inbound_webhook:
        a signed body is still external input. A status without a usable id or
        a known status name is dropped; a bad timestamp or error code only
        becomes None."""
        out = []
        for value in _values(payload):
            statuses = value.get("statuses")
            for status in statuses if isinstance(statuses, list) else []:
                if not isinstance(status, dict):
                    continue
                name = status.get("status")
                mapped = _STATUS_MAP.get(name) if isinstance(name, str) else None
                message_id = _id_text(status.get("id"))
                if mapped is None or message_id is None:
                    continue
                try:
                    occurred_at = datetime.fromtimestamp(int(status["timestamp"]), tz=UTC)
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    occurred_at = None
                errors = status.get("errors")
                first = errors[0] if isinstance(errors, list) and errors else None
                code = first.get("code") if isinstance(first, dict) else None
                if isinstance(code, bool) or not isinstance(code, int):
                    code = None
                out.append(StatusUpdate(message_id, mapped, occurred_at, code))
        return out

    def submit_template(self, account, template) -> str:
        components = [{"type": "BODY", "text": template.body}]
        examples = named_examples(template.body)
        if examples:
            components[0]["example"] = {"body_text_named_params": examples}
        result = _request(
            "POST",
            f"/{_quote(account.business_account_id)}/message_templates",
            {
                "name": provider_template_name(template),
                "category": _TEMPLATE_CATEGORY,
                "language": template.language,
                "parameter_format": "NAMED",
                "components": components,
            },
            operation="template submit",
            token=_token_for(account),
        )
        template_id = result.get("id")
        if not template_id:
            raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
        return str(template_id)

    def find_template_id(self, account, template) -> str | None:
        """Meta's template list (GET /{WABA}/message_templates) has no name
        filter (M-5f), so page through it and match the derived name and
        language. Called only after a permanent refusal, so it is rare.

        Three outcomes, never to be confused:
        - the provider id: the template is already on Meta;
        - None: the list was scanned to its end and the template is not there
          (the only result that lets the caller treat it as not submitted);
        - ProviderTransientError: the scan is inconclusive (page cap reached,
          paging unreadable, or the match has no id), so nothing may be
          concluded. Request failures raise ProviderTransientError or
          ProviderPermanentError out of _request unchanged.

        # ponytail: linear scan of the shared WABA, capped at
        # _FIND_MAX_PAGES pages; upgrade path is storing the derived name
        # before submitting, or a provider-side filter if Meta adds one.
        """
        name = provider_template_name(template)
        after = None
        for _ in range(_FIND_MAX_PAGES):
            query = {"fields": "id,name,language", "limit": str(_FIND_PAGE_SIZE)}
            if after:
                query["after"] = after
            page = _request(
                "GET",
                f"/{_quote(account.business_account_id)}/message_templates?{urllib.parse.urlencode(query)}",
                operation="template lookup",
                token=_token_for(account),
            )
            for item in page.get("data") or []:
                if isinstance(item, dict) and item.get("name") == name and item.get("language") == template.language:
                    if not item.get("id"):
                        raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
                    return str(item["id"])
            paging = page.get("paging") or {}
            if not paging.get("next"):
                return None  # the end of the list: genuinely not there
            after = (paging.get("cursors") or {}).get("after")
            if not after:
                raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
        logger.warning("Meta template lookup stopped at the page cap")
        raise ProviderTransientError("The WhatsApp template lookup was inconclusive.")

    def fetch_template_status(self, account, provider_template_id: str) -> TemplateStatus:
        status = _request(
            "GET",
            f"/{_quote(provider_template_id)}?fields=status",
            operation="template status",
            token=_token_for(account),
        ).get("status")
        if status == "APPROVED":
            return TemplateStatus("APPROVED")
        if status in _REJECTED_STATES:
            return TemplateStatus("REJECTED")
        return TemplateStatus("PENDING")

    def fetch_quality_rating(self, account) -> str:
        result = _request(
            "GET",
            f"/{_quote(account.business_account_id)}/phone_numbers?fields=id,quality_rating",
            operation="quality rating",
            token=_token_for(account),
        )
        for number in result.get("data") or []:
            if isinstance(number, dict) and number.get("id") == account.phone_number_id:
                return str(number.get("quality_rating") or "UNKNOWN")
        return "UNKNOWN"

    def send(self, account, to: str, template, variables: dict) -> ProviderSendResult:
        parameters = [
            {"type": "text", "parameter_name": name, "text": str(variables[name])}
            for name in dict.fromkeys(_PLACEHOLDER.findall(template.body))
        ]
        result = _request(
            "POST",
            f"/{_quote(account.phone_number_id)}/messages",
            {
                "messaging_product": "whatsapp",
                "to": to.lstrip("+"),
                "type": "template",
                "template": {
                    "name": provider_template_name(template),
                    "language": {"code": template.language},
                    "components": [{"type": "body", "parameters": parameters}],
                },
            },
            operation="send",
            token=_token_for(account),
        )
        messages = result.get("messages") or []
        if not messages or not isinstance(messages[0], dict) or not messages[0].get("id"):
            raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
        return ProviderSendResult(str(messages[0]["id"]))

    def register_number(self, credential: ProviderCredential, phone_number_id: str, pin: str) -> None:
        """POST /{phone_number_id}/register with the merchant's two-step PIN
        (WhatsApp-Meta.md E-6a, VERIFIED). Called inline from the Embedded
        Signup completion with the in-memory credential (the ES-4 exception)
        and never retried automatically: Meta allows 10 register calls per
        number per 72 hours (E-6c). Whether registering an already-registered
        number is harmless is unverified (E-6e).

        The PIN is only a request-body value here: it is not logged, not part
        of any exception text, and not kept."""
        result = _request(
            "POST",
            f"/{_quote(phone_number_id)}/register",
            {"messaging_product": "whatsapp", "pin": pin},
            operation="register number",
            token=credential.access_token,
        )
        if result.get("success") is not True:
            raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")

    def subscribe_app(self, account) -> None:
        """POST /{WABA}/subscribed_apps with the account's own token
        (WhatsApp-Meta.md E-5a, VERIFIED endpoint and response). Subscribing
        an app that is already subscribed is expected to be harmless but is
        not documented."""
        result = _request(
            "POST",
            f"/{_quote(account.business_account_id)}/subscribed_apps",
            operation="subscribe app",
            token=_token_for(account),
        )
        if result.get("success") is not True:
            raise ProviderTransientError("The WhatsApp provider returned an unreadable response.")
