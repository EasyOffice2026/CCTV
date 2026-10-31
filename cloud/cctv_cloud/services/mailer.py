"""Outgoing email (password reset, invitations).

The email provider is not chosen yet; until then messages are written to the log, which
is enough for development and the pilot's few users.
"""

import logging
from functools import lru_cache
from typing import Protocol

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class LogMailer:
    def send(self, to: str, subject: str, body: str) -> None:
        log.info("email to %s: %s\n%s", to, subject, body)


@lru_cache
def get_mailer() -> Mailer:
    return LogMailer()
