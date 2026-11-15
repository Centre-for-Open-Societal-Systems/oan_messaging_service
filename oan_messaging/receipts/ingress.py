"""Authenticated 8028 status-callback ingress (MSG-05).

Authentication is mutual TLS performed **by this process**, never by a header:

* ``build_server_ssl_context`` requires a client certificate (``CERT_REQUIRED``) and trusts
  only the pinned 8028-issued certificates (current + previous during renewal) as anchors,
  with ``VERIFY_X509_PARTIAL_CHAIN``. A certificate that is not pinned fails the handshake.
* After the handshake, ``ReceiptProcessor`` also compares the SHA-256 fingerprint of the
  peer certificate taken from the TLS session (``ssl_object.getpeercert(binary_form=True)``)
  with the pins (guide 5.5). This is defense in depth.
* There is no code path that reads a certificate or fingerprint from an HTTP header (for
  example ``X-SSL-Client-Cert``), so a public or spoofed header cannot authenticate.
  Deploy this listener as the TLS endpoint 8028 connects to. Never put it behind a
  terminator that drops the client certificate.

Acknowledgement: the callback is validated, then **durably persisted** through the
accepted ``record_receipt``, and only then answered 200. A storage failure answers 503 so
that 8028 retries (1 min, 5 min, 15 min, 1 h, up to 6 h). Duplicate, delayed,
out-of-order, early (before the send response) and undocumented-status callbacks are
handled by the repository: they are recorded, transitions stay legal and raw status is
preserved. Raw phone numbers are never persisted or logged.

The HTTP handling is deliberately minimal and strict: one ``POST`` path, ``Content-Length``
required, no chunked bodies, bounded header and body sizes, bounded time and one request
per connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oan_messaging.contracts import DeliveryReceipt, normalize_et_mobile
from oan_messaging.storage.repository import PostgresMessageRepository

log = logging.getLogger(__name__)

EAT = timezone(timedelta(hours=3), "EAT")
DEFAULT_PATH = "/8028/sms-status"
MAX_HEAD_BYTES = 8 * 1024
MAX_BODY_BYTES = 4 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0

_SMS_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_EVENT_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_STATUS_RE = re.compile(r"^[\x20-\x7e]{1,64}$")
_JSON_MEDIA = re.compile(r"^application/json\s*(;\s*charset=utf-8\s*)?$", re.IGNORECASE)


class ReceiptRejected(ValueError):
    """Invalid callback payload. ``code`` is a fixed token; nothing from the body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------- certificates


def certificate_fingerprint(der: bytes) -> bytes:
    return hashlib.sha256(der).digest()


def load_pins(pem_paths: Iterable[Path]) -> frozenset[bytes]:
    """SHA-256 fingerprints of the pinned certificates (current + previous)."""
    pins: set[bytes] = set()
    for path in pem_paths:
        try:
            der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeDecodeError):
            raise ValueError("pinned certificate could not be read") from None
        pins.add(certificate_fingerprint(der))
    if not pins:
        raise ValueError("at least one pinned 8028 certificate is required")
    return frozenset(pins)


def build_server_ssl_context(
    server_cert: Path, server_key: Path, pinned_certs: Iterable[Path]
) -> ssl.SSLContext:
    """Server context that requires a client certificate chaining to a pinned 8028 cert."""
    pinned = list(pinned_certs)
    if not pinned:
        raise ValueError("at least one pinned 8028 certificate is required")
    if os.stat(server_key).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("server key must not be accessible by group or others (600)")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(certfile=str(server_cert), keyfile=str(server_key))
    except (ssl.SSLError, OSError):
        raise ValueError("server certificate and key could not be loaded") from None
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    for path in pinned:
        try:
            ctx.load_verify_locations(cafile=str(path))
        except (ssl.SSLError, OSError):
            raise ValueError("pinned certificate could not be loaded") from None
    return ctx


# ------------------------------------------------------------------------ parsing


def parse_callback(body: bytes, received_at: datetime) -> DeliveryReceipt:
    """8028 callback JSON -> ``DeliveryReceipt`` (in memory; the phone is never stored)."""
    if len(body) > MAX_BODY_BYTES:
        raise ReceiptRejected("payload_too_large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReceiptRejected("invalid_json") from None
    if not isinstance(data, dict):
        raise ReceiptRejected("invalid_json")

    sms_id: Any = data.get("sms_id")
    if isinstance(sms_id, bool):
        raise ReceiptRejected("invalid_sms_id")
    if isinstance(sms_id, int) and sms_id > 0:
        sms_id = str(sms_id)  # same lossless tolerance as the accepted send adapter
    if not isinstance(sms_id, str) or not _SMS_ID_RE.fullmatch(sms_id):
        raise ReceiptRejected("invalid_sms_id")

    phone = data.get("phone_number")
    if not isinstance(phone, str):
        raise ReceiptRejected("invalid_phone_number")
    try:
        recipient = normalize_et_mobile(phone)
    except ValueError:
        raise ReceiptRejected("invalid_phone_number") from None

    status = data.get("sms_status")
    if not isinstance(status, str) or not _STATUS_RE.fullmatch(status):
        raise ReceiptRejected("invalid_sms_status")

    raw_time = data.get("event_time")
    if not isinstance(raw_time, str) or not _EVENT_TIME_RE.fullmatch(raw_time):
        raise ReceiptRejected("invalid_event_time")
    try:
        event_time = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EAT)
    except ValueError:
        raise ReceiptRejected("invalid_event_time") from None

    return DeliveryReceipt(
        provider_message_id=sms_id,
        recipient=recipient,
        raw_status=status,
        event_time=event_time.astimezone(UTC),
        received_at=received_at,
    )


# ---------------------------------------------------------------------- processing


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    @staticmethod
    def json(status: int, payload: dict[str, str]) -> Response:
        return Response(status, json.dumps(payload, separators=(",", ":")).encode())


class ReceiptProcessor:
    """Transport-independent core: given the peer certificate from the TLS session (or
    None), the request line, headers and body, produce the response. Headers are only used
    for Content-Type; they never authenticate."""

    def __init__(
        self,
        repo: PostgresMessageRepository,
        pins: frozenset[bytes],
        *,
        path: str = DEFAULT_PATH,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not pins:
            raise ValueError("pins required")
        self._repo = repo
        self._pins = pins
        self._path = path
        self._clock = clock

    def is_authenticated(self, peer_der: bytes | None) -> bool:
        """Peer certificate (from the TLS session only) matches a pinned fingerprint."""
        if not peer_der:
            return False
        fp = certificate_fingerprint(peer_der)
        return any(hmac.compare_digest(fp, pin) for pin in self._pins)

    async def handle(
        self, peer_der: bytes | None, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> Response:
        if not self.is_authenticated(peer_der):
            log.warning("callback rejected: client certificate not pinned")
            return Response.json(403, {"error": "forbidden"})
        if path != self._path:
            return Response.json(404, {"error": "not_found"})
        if method != "POST":
            return Response.json(405, {"error": "method_not_allowed"})
        if not _JSON_MEDIA.fullmatch(headers.get("content-type", "").strip()):
            return Response.json(415, {"error": "unsupported_media_type"})
        now = self._clock()
        try:
            receipt = parse_callback(body, now)
        except ReceiptRejected as exc:
            log.info("callback rejected code=%s", exc.code)
            status = 413 if exc.code == "payload_too_large" else 400
            return Response.json(status, {"error": exc.code})
        try:
            disposition = await self._repo.record_receipt(receipt, now)
        except Exception as exc:
            # Not persisted -> not acknowledged: 8028 will retry.
            log.error("receipt not persisted error_type=%s", type(exc).__name__)
            return Response.json(503, {"error": "retry_later"})
        log.info(
            "receipt persisted disposition=%s sms_status=%s", disposition.value, receipt.raw_status
        )
        return Response.json(200, {"status": "received"})


# ------------------------------------------------------------------------ server

_REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    431: "Headers Too Large",
    501: "Not Implemented",
    503: "Service Unavailable",
}


class CallbackServer:
    """Minimal HTTPS/1.1 server for the callback path, with mTLS pinned in-process."""

    def __init__(self, processor: ReceiptProcessor, ssl_context: ssl.SSLContext) -> None:
        if ssl_context.verify_mode is not ssl.CERT_REQUIRED:
            raise ValueError("callback ingress requires client certificates")
        self._processor = processor
        self._ssl = ssl_context
        self._server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(
            self._handle,
            host,
            port,
            ssl=self._ssl,
            limit=MAX_HEAD_BYTES,
            ssl_handshake_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            response = await asyncio.wait_for(
                self._serve(reader, writer), timeout=REQUEST_TIMEOUT_SECONDS
            )
        except TimeoutError:
            response = Response.json(408, {"error": "timeout"})
        except Exception as exc:
            log.error("callback connection error error_type=%s", type(exc).__name__)
            response = Response.json(400, {"error": "bad_request"})
        try:
            writer.write(
                f"HTTP/1.1 {response.status} {_REASONS.get(response.status, 'Error')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response.body)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + response.body
            )
            await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Response:
        ssl_object = writer.get_extra_info("ssl_object")
        peer_der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            return Response.json(431, {"error": "headers_too_large"})
        except asyncio.IncompleteReadError:
            return Response.json(400, {"error": "bad_request"})
        if len(head) > MAX_HEAD_BYTES:
            return Response.json(431, {"error": "headers_too_large"})
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            return Response.json(400, {"error": "bad_request"})
        method, target = parts[0], parts[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep:
                return Response.json(400, {"error": "bad_request"})
            headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers:
            return Response.json(501, {"error": "chunked_not_supported"})
        length = headers.get("content-length")
        if method == "POST":
            if length is None or not length.isdigit():
                return Response.json(411, {"error": "length_required"})
            if int(length) > MAX_BODY_BYTES:
                # Authenticate first so unauthenticated clients learn nothing more.
                if not self._processor.is_authenticated(peer_der):
                    return Response.json(403, {"error": "forbidden"})
                return Response.json(413, {"error": "payload_too_large"})
            body = await reader.readexactly(int(length))
        else:
            body = b""
        return await self._processor.handle(peer_der, method, target, headers, body)
