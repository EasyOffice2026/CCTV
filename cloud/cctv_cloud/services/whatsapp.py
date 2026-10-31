"""WhatsApp Business Platform (Cloud API) messages (plan 6.3).

Businesses may only start a conversation with a template that Meta has approved, so every
alert and report link is a template message: body parameters plus a URL button whose
suffix is the event or report id (the link opens the dashboard, which requires a login).
No images are sent: snapshots stay in our storage.

Without a token (development) messages are written to the log instead.
"""

import hashlib
import hmac
import logging
import re
import uuid
from functools import lru_cache
from typing import Protocol

import httpx

from cctv_cloud.config import get_settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
# Template parameters may not contain new lines, tabs or more than four spaces in a row.
_SPACES = re.compile(r"\s+")


class WhatsAppError(Exception):
    def __init__(self, message: str, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


class WhatsApp(Protocol):
    configured: bool

    def send_template(
        self, to: str, template: str, language: str, params: list[str], button_suffix: str
    ) -> str:
        """Returns the message id; raises WhatsAppError."""
        ...


def clean_param(value: str, limit: int = 200) -> str:
    return _SPACES.sub(" ", value).strip()[:limit] or "-"


class CloudApiWhatsApp:
    configured = True

    def __init__(self, base_url: str, phone_number_id: str, token: str) -> None:
        self._url = f"{base_url.rstrip('/')}/{phone_number_id}/messages"
        self._token = token

    def send_template(
        self, to: str, template: str, language: str, params: list[str], button_suffix: str
    ) -> str:
        body = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": clean_param(p)} for p in params],
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": button_suffix}],
                    },
                ],
            },
        }
        try:
            response = httpx.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"WhatsApp API unreachable: {exc}", retry=True) from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
                detail = f"{error.get('code')}: {error.get('message')}"
            except ValueError:
                detail = response.text[:200]
            # Rate limits and server errors are worth retrying; a bad number or template isn't.
            retry = response.status_code == 429 or response.status_code >= 500
            raise WhatsAppError(f"WhatsApp API {response.status_code} {detail}", retry=retry)
        return str(response.json()["messages"][0]["id"])


class LogWhatsApp:
    configured = False

    def send_template(
        self, to: str, template: str, language: str, params: list[str], button_suffix: str
    ) -> str:
        log.info("whatsapp to %s: %s/%s %s [%s]", to, template, language, params, button_suffix)
        return f"log.{uuid.uuid4()}"


def valid_signature(body: bytes, header: str | None) -> bool:
    """Checks X-Hub-Signature-256 on a webhook call (HMAC-SHA256 with the app secret)."""
    secret = get_settings().whatsapp_app_secret.get_secret_value()
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@lru_cache
def get_whatsapp() -> WhatsApp:
    settings = get_settings()
    token = settings.whatsapp_token.get_secret_value()
    if token and settings.whatsapp_phone_number_id:
        return CloudApiWhatsApp(settings.whatsapp_api_url, settings.whatsapp_phone_number_id, token)
    return LogWhatsApp()
