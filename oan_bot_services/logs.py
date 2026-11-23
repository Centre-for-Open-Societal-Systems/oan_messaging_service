"""Log redaction for Bot Services (§5.4): no message text, phone number, chat id, Farmer ID,
code or token in logs.

``install`` touches only Bot Services' own loggers; ``install_process`` (the Bot Services
process itself) also filters the root handlers and keeps ``httpx`` quiet.

Code logs only ``contact_ref``, ids it generated and fixed words. This filter is the second
line: it rewrites every record on the way out, replacing configured secrets, anything
token-shaped, long digit runs (chat ids, phones, codes of 4+ digits) and Farmer-ID-shaped
values.
"""

from __future__ import annotations

import logging
import re
import threading

_LOCK = threading.RLock()
_SECRETS: set[str] = set()
_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])(?:bot)?[0-9]{5,16}:[A-Za-z0-9_-]{30,64}"),  # bot token
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bFR-[A-Za-z0-9-]+"),  # Farmer ID
    re.compile(r"\+?\d[\d -]{3,}\d"),  # phones, chat ids, OTP codes (4+ digits)
)
_HEX_REF = re.compile(r"\b[0-9a-f]{64}\b")


def clean(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    # contact_ref (64 hex) and UUIDs may contain digit runs: protect them first.
    keep: list[str] = []

    def stash(m: re.Match[str]) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    text = _HEX_REF.sub(stash, text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", stash, text)
    for pattern in _PATTERNS:
        text = pattern.sub("<redacted>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "<unformattable log record>"
        record.msg = clean(message)
        record.args = None
        if record.exc_info:
            record.exc_text = clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            # Another filter (e.g. OAN Messaging's, in the same process) already rendered it.
            record.exc_text = clean(record.exc_text)
        return True


FILTER = RedactingFilter()
PACKAGE = "oan_bot_services"


def install(*secrets: str) -> None:
    """Register secrets and filter Bot Services' own loggers. Changes nothing outside
    ``oan_bot_services`` (safe when the package is imported next to other code)."""
    with _LOCK:
        _SECRETS.update(s for s in secrets if s)
    logger = logging.getLogger(PACKAGE)
    if FILTER not in logger.filters:
        logger.addFilter(FILTER)


def install_process() -> None:
    """For a Bot Services process only (``python -m oan_bot_services``, the API factory):
    filter every root handler, and keep ``httpx`` request logging off (a beneficiary URL
    holds the Farmer ID). ``uvicorn.access`` is left alone: its formatter reads the
    record's arguments, and Bot Services' paths carry no personal data."""
    install()
    root = logging.getLogger()
    for handler in root.handlers:
        if FILTER not in handler.filters:
            handler.addFilter(FILTER)
    for name in ("httpx", "httpcore"):
        if logging.getLogger(name).getEffectiveLevel() < logging.WARNING:
            logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn.error", "sqlalchemy", "alembic"):
        logger = logging.getLogger(name)
        if FILTER not in logger.filters:
            logger.addFilter(FILTER)


def get(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if FILTER not in logger.filters:
        logger.addFilter(FILTER)
    return logger
