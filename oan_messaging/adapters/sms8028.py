"""8028 SMS Gateway transport (MSG-03) implementing the frozen ``MessageAdapter`` contract.

See ``docs/8028-contract.md`` (summary of the supplied integration guide and its open items).

Request (``send``: one recipient per call, so one message is one ``sms_id``)::

    POST {base_url}/sms/send-sms        Content-Type: application/json; charset=utf-8
    {"clientSecret": "...", "smstext": "...", "phonenums": ["09XXXXXXXX"],
     "pushdatetime": "YYYY-MM-DD HH:MM:SS"}            # now, in EAT (UTC+3)

``send_batch`` puts several messages with the same text in one request (``phonenums`` has
one number per message). 8028 answers with one ``data`` entry per number, in the order
sent (guide v1.1 §5.2), and each message keeps its own ``sms_id``. The worker uses it only
for broadcast and alert messages, and only when batching is turned on.

Transport security:

* mTLS: the 8028-issued client certificate and key are loaded into the TLS context.
* The server certificate and hostname are always verified (system CAs, or the optional
  8028 ``ca.crt``). TLS 1.2 is the minimum. There is no option to disable verification.
* ``clientSecret`` goes only in the request body. It comes from secret configuration and
  never appears in logs, results or exceptions.
* Exactly one HTTP request per ``send``. Redirects are never followed, even if an injected
  client enables them. There are no internal retries.

Outcome classification (``ProviderResult``):

* ``sent``: a 2xx with ``success: true`` and exactly one ``data`` entry per number, each
  with that number as ``phone_number`` and a valid ``sms_id``. The ``sms_id`` is
  preserved. This means queued at 8028; it is not a delivery claim. Guide v1.1 answers
  201; the 12-page guide shows 200; any 2xx is read the same way.
* ``rejected``: definite refusals (400 invalid number or text, 413, other 4xx). In a batch
  a 413 is instead ``not_sent`` and retried: nothing was queued, and the worker then sends
  those messages one by one (12-page guide §4.4, "split into smaller batches").
* ``not_sent``: provably never queued. Connection refused (retryable). TLS or handshake
  failure, 401/403 credentials (not retryable).
* ``not_sent``, retryable: HTTP 500. 8028 confirmed in writing (20 Nov 2026) that a 500
  queues nothing, so the worker retries it with its normal backoff.
* ``unknown``: a 2xx body that is malformed or partial, any other 5xx (503 and the rest
  are not confirmed; we never resend blindly), a timeout after the request
  may have been written, an unexpected transport error, a redirect, or a 429. Never
  retryable.

429 is not documented by 8028, and nothing confirms that a rate-limit response means the
submission was not queued. Until the gateway owner confirms pre-queue rejection, a 429 is
``unknown`` / ``rate_limited``, not retryable, with no ``retry_after_seconds`` (the
contract reserves that for ``not_sent``). Retry-After is ignored. There is deliberately no
switch to enable optimistic retries.

``provider_status`` keeps the gateway's original HTTP status as ``http_<code>`` (or a
transport token). The gateway's ``message`` text can contain the phone number, so it is
matched on but never copied.

Message length (pilot rule): exactly one SMS part. That is 160 GSM-7 septets (extension
characters count 2), otherwise 70 UTF-16 units. The guide's three-part limits (466/204)
contradict its per-part sizes (3x153 = 459, 3x67 = 201), so multi-part is refused until
8028 confirms the real limit (OPEN in docs/8028-contract.md).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from oan_messaging.contracts import (
    SMS_MAX_PARTS,
    Channel,
    FailureCode,
    Provider,
    ProviderOutcome,
    ProviderResult,
    SendRequest,
    SmsSendRequest,
    normalize_et_mobile,
    sms_parts,
)

__all__ = [
    "BATCH_TOO_LARGE",
    "DEFAULT_BASE_URL",
    "EAT",
    "ENV_PREFIX",
    "Sms8028Adapter",
    "Sms8028ConfigError",
    "Sms8028Settings",
    "build_ssl_context",
    "eat_pushdatetime",
    "make_client",
    "to_domestic",
]

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://8028.et:3400/sms-api/v1"
SEND_PATH = "/sms/send-sms"
ENV_PREFIX = "OAN_MSG_SMS8028_"
#: East Africa Time. Ethiopia observes no daylight saving time.
EAT = timezone(timedelta(hours=3), "EAT")

#: ``detail`` of a batch refused with 413: the worker sends these messages one by one.
BATCH_TOO_LARGE = "batch too large; sent one by one on retry"

_E164_RE = re.compile(r"^\+251([79][0-9]{8})$")
_SMS_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")


class Sms8028ConfigError(ValueError):
    """Invalid gateway configuration. Messages never include secrets or key material."""


# --------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Sms8028Settings:
    """Gateway configuration from secret storage (vault-injected environment)."""

    client_secret: SecretStr
    client_cert: Path
    client_key: Path
    ca_cert: Path | None = None
    base_url: str = DEFAULT_BASE_URL
    connect_timeout: float = 5.0
    read_timeout: float = 15.0

    def __post_init__(self) -> None:
        secret = self.client_secret.get_secret_value()
        if not secret or secret != secret.strip():
            raise Sms8028ConfigError("client secret must be non-empty without surrounding space")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise Sms8028ConfigError("base_url must be an https URL")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise Sms8028ConfigError("base_url must not contain query, fragment or credentials")
        if not (0 < self.connect_timeout <= 60 and 0 < self.read_timeout <= 120):
            raise Sms8028ConfigError("timeouts must be positive and bounded")

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + SEND_PATH

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Sms8028Settings:
        """``OAN_MSG_SMS8028_{CLIENT_SECRET,CLIENT_CERT,CLIENT_KEY,CA_CERT,BASE_URL}``."""

        def get(name: str) -> str | None:
            value = environ.get(ENV_PREFIX + name)
            return value.strip() if value is not None and value.strip() else None

        secret, cert, key = get("CLIENT_SECRET"), get("CLIENT_CERT"), get("CLIENT_KEY")
        if secret is None or cert is None or key is None:
            raise Sms8028ConfigError("CLIENT_SECRET, CLIENT_CERT and CLIENT_KEY are required")
        ca = get("CA_CERT")
        return cls(
            client_secret=SecretStr(secret),
            client_cert=Path(cert),
            client_key=Path(key),
            ca_cert=Path(ca) if ca else None,
            base_url=get("BASE_URL") or DEFAULT_BASE_URL,
        )

    def __repr__(self) -> str:
        return f"Sms8028Settings(endpoint={self.endpoint!r}, client_secret=<redacted>)"


def build_ssl_context(settings: Sms8028Settings) -> ssl.SSLContext:
    """mTLS context: verified server certificate and hostname, TLS >= 1.2, and the
    8028-issued client certificate. Refuses a private key readable by group or others."""
    for path in (settings.client_cert, settings.client_key, settings.ca_cert):
        if path is not None and not path.is_file():
            raise Sms8028ConfigError("configured certificate/key file is missing")
    mode = os.stat(settings.client_key).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Sms8028ConfigError("client key must not be accessible by group or others (600)")
    ctx = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=str(settings.ca_cert) if settings.ca_cert else None,
    )
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ctx.load_cert_chain(certfile=str(settings.client_cert), keyfile=str(settings.client_key))
    except (ssl.SSLError, OSError, ValueError):
        # Do not chain: OpenSSL messages can echo file details.
        raise Sms8028ConfigError(
            "client certificate and key could not be loaded together"
        ) from None
    return ctx


def make_client(settings: Sms8028Settings) -> httpx.AsyncClient:
    """Production client: mTLS context, no redirects, no environment proxies, bounded
    timeouts. Callers own its lifecycle (``await client.aclose()``)."""
    return httpx.AsyncClient(
        verify=build_ssl_context(settings),
        timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
        follow_redirects=False,
        trust_env=False,
    )


# ------------------------------------------------------------------------- helpers


def to_domestic(e164: str) -> str:
    """``+2519XXXXXXXX`` -> ``09XXXXXXXX`` (and 7-series). Raises ValueError otherwise."""
    m = _E164_RE.fullmatch(e164)
    if m is None:
        raise ValueError("recipient is not an Ethiopian 9/7-series mobile in E.164")
    return "0" + m.group(1)


def eat_pushdatetime(moment: datetime) -> str:
    """Aware datetime -> ``YYYY-MM-DD HH:MM:SS`` in EAT (seconds truncated, never rounded
    up, so 'now' is never a future time that would schedule the message)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("pushdatetime requires an aware datetime")
    return moment.astimezone(EAT).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _has_cause(exc: BaseException, kinds: tuple[type[BaseException], ...]) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, kinds):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


class _SecretRedactingFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def covers(self, secret: str) -> bool:
        return self._secret == secret

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if self._secret in message:
            record.msg = message.replace(self._secret, "<redacted>")
            record.args = None
        return True


# ------------------------------------------------------------------------- adapter


class Sms8028Adapter:
    """``MessageAdapter`` for the 8028 Send SMS API."""

    provider: ClassVar[Provider] = Provider.SMS_8028
    channel: ClassVar[Channel] = Channel.SMS

    def __init__(
        self,
        settings: Sms8028Settings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock
        self._secret = settings.client_secret.get_secret_value()
        from oan_messaging.redaction import install_redaction

        install_redaction(self._secret)
        for name in ("httpx", "httpcore", __name__):
            logger = logging.getLogger(name)
            if not any(
                isinstance(f, _SecretRedactingFilter) and f.covers(self._secret)
                for f in logger.filters
            ):
                logger.addFilter(_SecretRedactingFilter(self._secret))

    def __repr__(self) -> str:
        return f"Sms8028Adapter(endpoint={self._settings.endpoint!r})"

    async def send(self, request: SendRequest) -> ProviderResult:
        started = self._clock()
        try:
            result = await self._send(request, started)
        except Exception as exc:  # contract: never raise; never leak via str(exc)
            result = self._result(
                started,
                ProviderOutcome.UNKNOWN,
                "adapter_error",
                failure=FailureCode.INTERRUPTED,
                detail=f"unexpected {type(exc).__name__}",
            )
        log.info(
            "sms8028 send message_id=%s attempt=%s outcome=%s provider_status=%s failure_code=%s",
            request.message_id,
            request.attempt,
            result.outcome.value,
            result.provider_status,
            result.failure_code.value if result.failure_code else None,
        )
        return result

    # -- pre-flight -------------------------------------------------------------------

    def _preflight(self, request: SendRequest, started: datetime) -> ProviderResult | str:
        """A result when the request must not go out, else the 09/07 domestic number."""
        if started.tzinfo is None or started.utcoffset() is None:
            return self._result(
                started.replace(tzinfo=UTC),
                ProviderOutcome.NOT_SENT,
                "clock_not_aware",
                failure=FailureCode.PROVIDER_CONFIG,
                detail="adapter clock must be timezone-aware",
            )
        if not isinstance(request, SmsSendRequest):
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                "wrong_channel",
                failure=FailureCode.PROVIDER_CONFIG,
                detail="non-sms request routed to the 8028 adapter",
            )
        try:
            domestic = to_domestic(request.recipient)
        except ValueError:
            return self._result(
                started,
                ProviderOutcome.REJECTED,
                "invalid_recipient",
                failure=FailureCode.INVALID_RECIPIENT,
                detail="recipient is not a 09/07 mobile",
            )
        text = request.text
        if not text.strip() or _CONTROL.search(text):
            return self._result(
                started,
                ProviderOutcome.REJECTED,
                "invalid_text",
                failure=FailureCode.TEXT_REJECTED,
                detail="text is blank or has control characters",
            )
        if sms_parts(text) > SMS_MAX_PARTS:
            return self._result(
                started,
                ProviderOutcome.REJECTED,
                "text_too_long",
                failure=FailureCode.TEXT_REJECTED,
                detail=f"text exceeds {SMS_MAX_PARTS} SMS parts",
            )
        if request.expires_at is not None and request.expires_at <= started:
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                "expired",
                failure=FailureCode.EXPIRED_BEFORE_SEND,
                detail="expired before the provider call",
            )
        return domestic

    async def _send(self, request: SendRequest, started: datetime) -> ProviderResult:
        checked = self._preflight(request, started)
        if isinstance(checked, ProviderResult):
            return checked
        assert isinstance(request, SmsSendRequest)
        response = await self._post(request.text, [checked], started)
        if isinstance(response, ProviderResult):
            return response
        return self._classify(started, checked, response)

    async def _post(
        self, text: str, domestic: list[str], started: datetime
    ) -> httpx.Response | ProviderResult:
        """Exactly one HTTP request. A transport failure comes back as the result that
        applies to every number in it."""
        body = json.dumps(
            {
                "clientSecret": self._secret,
                "smstext": text,
                "phonenums": domestic,
                # Guide v1.1 §5.4: a date before today is refused, so always send now.
                "pushdatetime": eat_pushdatetime(started),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            return await self._client.post(
                self._settings.endpoint,
                content=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
                follow_redirects=False,
            )
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                "connect_timeout",
                failure=FailureCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                detail="connection not established",
            )
        except httpx.ConnectError as exc:
            if _has_cause(exc, (ssl.SSLError, ConnectionResetError)):
                # Handshake failed or was reset (8028 rejects bad/expired/revoked client
                # certificates this way). Nothing was sent; fix configuration, no retry.
                return self._result(
                    started,
                    ProviderOutcome.NOT_SENT,
                    "tls_error",
                    failure=FailureCode.PROVIDER_CONFIG,
                    detail="TLS handshake failed or was reset",
                )
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                "conn_refused",
                failure=FailureCode.PROVIDER_UNAVAILABLE,
                retryable=True,
                detail="connection refused or DNS failure",
            )
        except httpx.TimeoutException:
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                "timeout",
                failure=FailureCode.TIMEOUT,
                detail="no response in time; request may have been queued",
            )
        except httpx.HTTPError as exc:
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                "transport_error",
                failure=FailureCode.MALFORMED_RESPONSE,
                detail=f"transport {type(exc).__name__}",
            )

    # -- batching ---------------------------------------------------------------------

    async def send_batch(self, requests: Sequence[SmsSendRequest]) -> list[ProviderResult]:
        """One 8028 request for several messages with the same text (guide v1.1 §5.1-5.2).

        Returns one result per request, in order. Requests that fail the pre-flight
        checks get their own result and are left out of the call. The rest share one
        request, so they share its outcome, except on success, where each gets its own
        ``sms_id``. 8028 answers with one ``data`` entry per number, in the order sent
        (§5.2); every entry must be there and match its number, otherwise nothing can be
        proven and every message in the batch is ``unknown``. A 413 means nothing was
        queued: the messages are retried, and the worker then sends them one by one.
        Never raises."""
        started = self._clock()
        results: list[ProviderResult | None] = [None] * len(requests)
        try:
            outgoing: list[tuple[int, str]] = []
            for i, request in enumerate(requests):
                checked = self._preflight(request, started)
                if isinstance(checked, ProviderResult):
                    results[i] = checked
                else:
                    outgoing.append((i, checked))
            texts = {requests[i].text for i, _ in outgoing}
            if len(texts) > 1:
                shared = self._result(
                    started,
                    ProviderOutcome.NOT_SENT,
                    "batch_mixed_text",
                    failure=FailureCode.PROVIDER_CONFIG,
                    detail="a batch must have one text",
                )
                for i, _ in outgoing:
                    results[i] = shared
                outgoing = []
            if outgoing:
                numbers = [d for _, d in outgoing]
                response = await self._post(requests[outgoing[0][0]].text, numbers, started)
                if isinstance(response, ProviderResult):
                    batch = [response] * len(outgoing)
                else:
                    batch = self._classify_batch(started, numbers, response)
                for (i, _), result in zip(outgoing, batch, strict=True):
                    results[i] = result
        except Exception as exc:  # contract: never raise; never leak via str(exc)
            failed = self._result(
                started,
                ProviderOutcome.UNKNOWN,
                "adapter_error",
                failure=FailureCode.INTERRUPTED,
                detail=f"unexpected {type(exc).__name__}",
            )
            results = [r if r is not None else failed for r in results]
        final = [r for r in results if r is not None]
        assert len(final) == len(requests)
        for request, result in zip(requests, final, strict=True):
            log.info(
                "sms8028 batch send message_id=%s attempt=%s batch_size=%s outcome=%s "
                "provider_status=%s failure_code=%s",
                request.message_id,
                request.attempt,
                len(requests),
                result.outcome.value,
                result.provider_status,
                result.failure_code.value if result.failure_code else None,
            )
        return final

    def _classify_batch(
        self, started: datetime, numbers: list[str], response: httpx.Response
    ) -> list[ProviderResult]:
        status = response.status_code
        tag = f"http_{status}"
        if status == 413:
            # 12-page guide §4.4: "split into smaller batches". Nothing was queued.
            retry = self._result(
                started,
                ProviderOutcome.NOT_SENT,
                tag,
                failure=FailureCode.TEXT_REJECTED,
                retryable=True,
                detail=BATCH_TOO_LARGE,
            )
            return [retry] * len(numbers)
        if not 200 <= status <= 299:
            shared = self._classify(started, numbers[0], response)
            return [shared] * len(numbers)
        body = _json_object(response)

        def malformed(detail: str) -> list[ProviderResult]:
            # 8028 may have queued every number: never retry automatically.
            unknown = self._result(
                started,
                ProviderOutcome.UNKNOWN,
                tag,
                failure=FailureCode.MALFORMED_RESPONSE,
                detail=detail,
            )
            return [unknown] * len(numbers)

        if body is None:
            return malformed("success body is not a JSON object")
        if body.get("success") is not True:
            return malformed("success flag missing or not true")
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(numbers):
            return malformed("expected one data entry per number")
        ids: list[str] = []
        for entry, number in zip(data, numbers, strict=True):
            sms_id = _entry_sms_id(entry, number)
            if sms_id is None:
                return malformed("data entry missing, out of order or invalid")
            ids.append(sms_id)
        if len(set(ids)) != len(ids):
            return malformed("sms_id repeated in one response")
        return [
            self._result(started, ProviderOutcome.SENT, tag, provider_message_id=sms_id)
            for sms_id in ids
        ]

    # -- response classification --------------------------------------------------------

    def _classify(
        self, started: datetime, domestic: str, response: httpx.Response
    ) -> ProviderResult:
        status = response.status_code
        tag = f"http_{status}"
        body = _json_object(response)

        if 200 <= status <= 299:
            return self._classify_success(started, domestic, body, tag)
        if status == 500:
            # 8028, 20 Nov 2026: "SMS will not be queued" on a 500 (guide v1.1 section 6).
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                tag,
                failure=FailureCode.PROVIDER_SERVER_ERROR,
                detail="gateway error 500; nothing queued (8028), retried",
                retryable=True,
            )
        if 500 <= status <= 599:
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                tag,
                failure=FailureCode.PROVIDER_SERVER_ERROR,
                detail="gateway error; outcome unknown, not resent",
            )
        if body is not None and body.get("success") is True:
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                tag,
                failure=FailureCode.MALFORMED_RESPONSE,
                detail="success:true with an error status",
            )
        if status in (401, 403):
            return self._result(
                started,
                ProviderOutcome.NOT_SENT,
                tag,
                failure=FailureCode.PROVIDER_AUTH,
                detail="gateway credentials rejected or suspended",
            )
        if status == 429:
            # Undocumented by 8028: non-queueing is unconfirmed, so never resend.
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                tag,
                failure=FailureCode.RATE_LIMITED,
                detail="gateway rate limit; non-queueing unconfirmed, not resent",
            )
        if status == 413:
            return self._result(
                started,
                ProviderOutcome.REJECTED,
                tag,
                failure=FailureCode.TEXT_REJECTED,
                detail="request too large",
            )
        if status == 400:
            failure, detail = _classify_400(body)
            return self._result(
                started, ProviderOutcome.REJECTED, tag, failure=failure, detail=detail
            )
        if 400 <= status <= 499:
            return self._result(
                started,
                ProviderOutcome.REJECTED,
                tag,
                failure=FailureCode.PROVIDER_REJECTED,
                detail="request rejected by the gateway",
            )
        # 1xx/3xx: redirects are never followed; outcome cannot be proven.
        return self._result(
            started,
            ProviderOutcome.UNKNOWN,
            tag,
            failure=FailureCode.MALFORMED_RESPONSE,
            detail="unexpected HTTP status",
        )

    def _classify_success(
        self, started: datetime, domestic: str, body: dict[str, Any] | None, tag: str
    ) -> ProviderResult:
        def malformed(detail: str) -> ProviderResult:
            # 8028 may have queued the message: never retry automatically.
            return self._result(
                started,
                ProviderOutcome.UNKNOWN,
                tag,
                failure=FailureCode.MALFORMED_RESPONSE,
                detail=detail,
            )

        if body is None:
            return malformed("success body is not a JSON object")
        if body.get("success") is not True:
            return malformed("success flag missing or not true")
        data = body.get("data")
        if not isinstance(data, list) or len(data) != 1:
            return malformed("expected exactly one data entry")
        sms_id = _entry_sms_id(data[0], domestic)
        if sms_id is None:
            return malformed("data entry invalid or for a different recipient")
        return self._result(started, ProviderOutcome.SENT, tag, provider_message_id=sms_id)

    # -- result construction ------------------------------------------------------------

    def _result(
        self,
        started: datetime,
        outcome: ProviderOutcome,
        provider_status: str,
        *,
        failure: FailureCode | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
        provider_message_id: str | None = None,
        detail: str | None = None,
    ) -> ProviderResult:
        finished = self._clock()
        if finished.tzinfo is None:
            finished = started
        return ProviderResult(
            outcome=outcome,
            provider=Provider.SMS_8028,
            provider_message_id=provider_message_id,
            provider_status=provider_status[:64],
            failure_code=failure,
            retryable=retryable,
            retry_after_seconds=retry_after,
            started_at=started,
            finished_at=max(finished, started),
            detail=None if detail is None else detail.replace(self._secret, "<redacted>")[:200],
        )


def _entry_sms_id(entry: object, domestic: str) -> str | None:
    """The ``sms_id`` of one ``data`` entry when it is for ``domestic``, else None."""
    if not isinstance(entry, dict):
        return None
    try:
        returned = normalize_et_mobile(str(entry.get("phone_number", "")))
    except ValueError:
        return None
    if returned != normalize_et_mobile(domestic):
        return None
    sms_id = entry.get("sms_id")
    if isinstance(sms_id, bool):
        return None
    if isinstance(sms_id, int) and sms_id > 0:
        sms_id = str(sms_id)
    if not isinstance(sms_id, str) or not _SMS_ID_RE.fullmatch(sms_id):
        return None
    return sms_id


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _classify_400(body: dict[str, Any] | None) -> tuple[FailureCode, str]:
    """Definite rejection. The gateway message is matched, never copied (it can contain
    the phone number)."""
    message = body.get("message") if body is not None else None
    text = message.lower() if isinstance(message, str) else ""
    if "phone" in text or "number" in text:
        return FailureCode.INVALID_RECIPIENT, "gateway rejected the recipient number"
    if "smstext" in text or "text" in text or "message" in text:
        return FailureCode.TEXT_REJECTED, "gateway rejected the message text"
    return FailureCode.PROVIDER_REJECTED, "gateway rejected the request"
