"""PostgreSQL implementation of the frozen ``MessageRepository`` (contract 3.0.0).

Concurrency model
-----------------
* Every public method runs in its own READ COMMITTED transaction and commits before it
  returns. ``create_or_get`` commits the message (metadata + encrypted content) before
  the API answers 202.
* Idempotency races are settled by the partial unique index ``uq_messages_live_binding``
  with ``INSERT ... ON CONFLICT DO NOTHING``: the loser waits for the winner, re-reads the
  committed row and classifies it (REPLAY or CONFLICT). Exactly one row per live binding.
* Workers claim with ``FOR UPDATE SKIP LOCKED`` and a lease token. Every later state change
  is conditional on that token; a stale worker gets ``LeaseLost`` and changes nothing.
* A database trigger rejects status transitions outside ``ALLOWED_TRANSITIONS``. CHECK
  constraints enforce the binding, fingerprint and content-retention invariants.
* Key retirement takes an exclusive advisory lock; every operation that uses a fingerprint
  or content key takes the shared lock and refuses retired versions, so a retirement
  cannot race a write that still needs the key.

Nothing here logs or raises with message text, recipients, fingerprints, keys or SQL
parameters (the engine should be created with ``hide_parameters=True``).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy import (
    Select,
    and_,
    any_,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    true,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    ALLOWED_TRANSITIONS,
    COMPLETED_BINDING_STATUSES,
    CONTENT_REQUIRED_STATUSES,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_RETRY_AFTER_SECONDS,
    PURGEABLE_STATUSES,
    PURPOSE_PRIORITY,
    RECEIPT_STATUS_MAP,
    CallerContext,
    Channel,
    Claim,
    DeliveryReceipt,
    FailureCode,
    FingerprintDomain,
    HmacFingerprinter,
    IdempotencyConflict,
    IdempotencyDecision,
    KeyedFingerprint,
    LeaseLost,
    MessageStatus,
    Provider,
    ProviderOutcome,
    ProviderResult,
    Purpose,
    ReceiptDisposition,
    ReceiptStatus,
    SendRequest,
    ServicePolicy,
    SmsSendRequest,
    SmsSubmission,
    StoredMessage,
    Submission,
    TelegramSendRequest,
    can_retire_key_version,
    canonical_payload,
    canonical_recipient,
    check_expiry,
    classify_idempotent_request,
    mask_chat_id,
    mask_recipient,
    parse_reply_markup,
    recipient_match_deadline,
    sms_parts,
    submission_channel,
)
from oan_messaging.storage.crypto import ContentCipher, EncryptedContent, KeyringError
from oan_messaging.storage.db import rowcount
from oan_messaging.storage.errors import (
    BindingIntegrityError,
    DispatchNotAuthorized,
    ExpiryRejected,
    KeyRetirementRefused,
    MessageExpired,
)
from oan_messaging.storage.schema import (
    callers,
    dispatch_controls,
    message_attempts,
    message_content,
    messages,
    receipts,
    retired_key_versions,
)

log = logging.getLogger(__name__)


@dataclass
class BeginProgress:
    """Set by ``begin_attempt``: ``commit_sent`` is true once COMMIT may have been issued."""

    commit_sent: bool = False


@dataclass(frozen=True)
class Reservation:
    """What a rate reservation (``reserve`` hook) decided inside the begin transaction.

    ``at`` is the shared effective time: it is the attempt's ``started_at`` and the time
    the lease and expiry are checked against. ``pace_total_until`` / ``pace_bulk_until``
    are the pace debt of a batch bigger than one window; they are written on every attempt
    row the transaction inserts (worker/rate_limit.py reads them back)."""

    at: datetime
    pace_total_until: datetime | None = None
    pace_bulk_until: datetime | None = None


def attempt_units(request: SendRequest) -> int:
    """Pace units one attempt uses: its SMS parts (an SMS of 3 parts is 3), Telegram 1."""
    if isinstance(request, SmsSendRequest):
        return sms_parts(request.text)
    return 1


#: A ``reserve`` hook may return a plain time (no ledger) or a ``Reservation``.
ReserveHook = Callable[[AsyncConnection], Awaitable[datetime | Reservation]]

#: Advisory lock key coordinating key retirement with key use ("OANK").
KEY_LOCK_ID = 0x4F414E4B
#: Two-key advisory lock class for (8028 sms_id) correlation ("OANR"). Lock order is
#: always: key lock (shared) -> receipt lock -> message row lock.
RECEIPT_LOCK_CLASS = 0x4F414E52


def dispatch_authorized() -> Any:
    """Caller row (joined/correlated) currently authorizes this message for dispatch."""
    return and_(
        callers.c.enabled.is_(True),
        messages.c.channel == any_(callers.c.allowed_channels),
        messages.c.purpose == any_(callers.c.allowed_purposes),
        or_(
            messages.c.channel == Channel.SMS.value,
            callers.c.telegram_bot == messages.c.authorized_bot,
        ),
    )


_PURGEABLE = sorted(s.value for s in PURGEABLE_STATUSES)
_COMPLETED = sorted(s.value for s in COMPLETED_BINDING_STATUSES)
_CONTENT_REQUIRED = sorted(s.value for s in CONTENT_REQUIRED_STATUSES)


def _expiry_code(message: str) -> str:
    if "required" in message:
        return "required_for_otp"
    if "future" in message and "far" not in message:
        return "not_in_future"
    return "beyond_ceiling"


def _fp(
    version: str | None, digest: str | None, domain: FingerprintDomain
) -> KeyedFingerprint | None:
    if version is None or digest is None:
        return None
    return KeyedFingerprint(key_version=version, domain=domain, digest=digest)


def row_to_message(row: Mapping[str, Any]) -> StoredMessage:
    """Validate a database row against the contract. Invalid rows are integrity errors."""
    try:
        return StoredMessage(
            id=row["id"],
            caller_id=row["caller_id"],
            channel=row["channel"],
            idempotency_key=row["idempotency_key"],
            purpose=row["purpose"],
            priority=row["priority"],
            source_reference=row["source_reference"],
            status=row["status"],
            failure_code=row["failure_code"],
            provider_status=row["provider_status"],
            provider_message_id=row["provider_message_id"],
            payload_fingerprint=_fp(
                row["payload_fp_version"], row["payload_fp_digest"], FingerprintDomain.PAYLOAD
            ),
            binding_released_at=row["binding_released_at"],
            recipient_fingerprint=_fp(
                row["recipient_fp_version"], row["recipient_fp_digest"], FingerprintDomain.RECIPIENT
            ),
            recipient_masked=row["recipient_masked"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            sent_at=row["sent_at"],
            delivered_at=row["delivered_at"],
            last_attempt_at=row["last_attempt_at"],
            not_before=row["not_before"],
            content_purged_at=row["content_purged_at"],
        )
    except ValidationError:
        # Do not chain the ValidationError: its text can include row values.
        raise BindingIntegrityError("stored message violates the contract") from None


def _broadcast_filter(allow: bool | Collection[Channel]) -> Any:
    """Claim filter for purpose ``broadcast``. A boolean gives exactly the pre-3.4.0
    statement; a channel collection allows broadcast on those channels only."""
    if allow is True:
        return true()
    if allow is False:
        return messages.c.purpose != "broadcast"
    allowed = sorted(Channel(c).value for c in allow)
    if not allowed:
        return messages.c.purpose != "broadcast"
    return or_(messages.c.purpose != "broadcast", messages.c.channel.in_(allowed))


class CancelOutcome(StrEnum):
    """Result of ``cancel_for_caller`` (contract 3.4.0)."""

    CANCELLED = "cancelled"  # cancelled now, or already cancelled: 200
    NOT_FOUND = "not_found"  # no such message for this caller: 404
    NOT_TELEGRAM = "not_telegram"  # an SMS message: 422
    NOT_CANCELLABLE = "not_cancellable"  # claimed, sending or finished: 409


class PostgresMessageRepository:
    """``MessageRepository`` on PostgreSQL. Also exposes key-retirement helpers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: ContentCipher,
        fingerprinter: HmacFingerprinter,
        policy: ServicePolicy,
    ) -> None:
        self._sf = session_factory
        self._cipher = cipher
        self._fp = fingerprinter
        self._policy = policy

    def __repr__(self) -> str:
        return "PostgresMessageRepository(<redacted>)"

    # ------------------------------------------------------------------ key guards

    @staticmethod
    async def _key_lock_shared(s: AsyncSession) -> None:
        await s.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KEY_LOCK_ID})

    @staticmethod
    async def _receipt_lock(s: AsyncSession, provider_message_id: str) -> None:
        """Serialize receipt persistence with send-result correlation for one sms_id, so
        a callback can never commit an orphan that the send transaction already scanned."""
        await s.execute(
            text("SELECT pg_advisory_xact_lock(:c, hashtext(:p))"),
            {"c": RECEIPT_LOCK_CLASS, "p": provider_message_id},
        )

    @staticmethod
    async def _ensure_not_retired(s: AsyncSession, kind: str, versions: Sequence[str]) -> None:
        found = await s.execute(
            select(retired_key_versions.c.key_version).where(
                retired_key_versions.c.kind == kind,
                retired_key_versions.c.key_version.in_(list(versions)),
            )
        )
        if found.first() is not None:
            raise KeyringError(f"{kind} key version has been retired")

    def _fingerprint(
        self, domain: FingerprintDomain, data: bytes, version: str | None = None
    ) -> KeyedFingerprint:
        try:
            return self._fp.fingerprint(domain, data, version)
        except ValueError:
            raise KeyringError("fingerprint key version not available") from None

    # -------------------------------------------------------------- acceptance

    async def create_or_get(
        self,
        caller: CallerContext,
        idempotency_key: str,
        submission: Submission,
        now: datetime,
    ) -> tuple[StoredMessage, IdempotencyDecision]:
        if not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, idempotency_key):
            raise ValueError("invalid idempotency key")
        channel = submission_channel(submission)
        async with self._sf() as s, s.begin():
            await self._key_lock_shared(s)
            # 1. Existing live binding first: no time-sensitive validation for replays.
            existing = await self._live_binding(s, caller.caller_id, channel, idempotency_key)
            if existing is not None:
                return await self._classify(s, existing, caller.caller_id, submission)

            # 2. New acceptance: expiry ceilings apply only here; expiry never modified.
            try:
                check_expiry(submission.purpose, submission.expires_at, now, self._policy)
            except ValueError as exc:
                raise ExpiryRejected(_expiry_code(str(exc))) from None

            await self._ensure_not_retired(s, "fingerprint", [self._fp.current_version])
            await self._ensure_not_retired(s, "content", [self._cipher.current_version])
            message_id = uuid.uuid4()
            payload_fp = self._fingerprint(
                FingerprintDomain.PAYLOAD, canonical_payload(caller.caller_id, submission)
            )
            if isinstance(submission, SmsSubmission):
                recipient_fp: KeyedFingerprint | None = self._fingerprint(
                    FingerprintDomain.RECIPIENT, canonical_recipient(submission.recipient)
                )
                masked = mask_recipient(submission.recipient)
                content: dict[str, Any] = {
                    "text": submission.text,
                    "recipient": submission.recipient,
                }
            else:
                if caller.telegram_bot is None:
                    raise ValueError("caller has no authorized telegram bot")
                recipient_fp = None
                masked = mask_chat_id(submission.chat_id)
                content = {"text": submission.text, "chat_id": submission.chat_id}
                # Contract 3.4.0: the buttons live in the encrypted content, so every path
                # that purges the text purges them in the same statement.
                if submission.reply_markup is not None:
                    content["reply_markup"] = submission.reply_markup.model_dump(mode="json")

            inserted = await s.execute(
                pg_insert(messages)
                .values(
                    id=message_id,
                    caller_id=caller.caller_id,
                    channel=channel.value,
                    idempotency_key=idempotency_key,
                    purpose=submission.purpose.value,
                    priority=PURPOSE_PRIORITY[submission.purpose],
                    source_reference=submission.source_reference,
                    status=MessageStatus.ACCEPTED.value,
                    payload_fp_version=payload_fp.key_version,
                    payload_fp_digest=payload_fp.digest,
                    recipient_fp_version=recipient_fp.key_version if recipient_fp else None,
                    recipient_fp_digest=recipient_fp.digest if recipient_fp else None,
                    recipient_masked=masked,
                    authorized_bot=(caller.telegram_bot if channel is Channel.TELEGRAM else None),
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                    expires_at=submission.expires_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        messages.c.caller_id,
                        messages.c.channel,
                        messages.c.idempotency_key,
                    ],
                    index_where=messages.c.binding_released_at.is_(None),
                )
                .returning(messages.c.id)
            )
            if inserted.scalar_one_or_none() is None:
                # A concurrent request won and committed; classify against its row.
                log.debug("idempotency insert lost a race; classifying the committed row")
                winner = await self._live_binding(s, caller.caller_id, channel, idempotency_key)
                if winner is None:
                    raise BindingIntegrityError("idempotency race left no live binding")
                return await self._classify(s, winner, caller.caller_id, submission)

            blob = self._cipher.encrypt(message_id, channel.value, content)
            await s.execute(
                message_content.insert().values(
                    message_id=message_id,
                    key_version=blob.key_version,
                    nonce=blob.nonce,
                    ciphertext=blob.ciphertext,
                    created_at=now,
                )
            )
            row = await self._get_row(s, message_id)
            assert row is not None
            stored = row_to_message(row)
        return stored, IdempotencyDecision.NEW

    async def _classify(
        self, s: AsyncSession, row: Mapping[str, Any], caller_id: str, submission: Submission
    ) -> tuple[StoredMessage, IdempotencyDecision]:
        if row["payload_fp_digest"] is None:
            # Live binding without a fingerprint: corruption, never "new".
            raise BindingIntegrityError("live binding has no payload fingerprint")
        stored = row_to_message(row)
        assert stored.payload_fingerprint is not None
        version = stored.payload_fingerprint.key_version
        await self._ensure_not_retired(s, "fingerprint", [version])
        candidate = self._fingerprint(
            FingerprintDomain.PAYLOAD, canonical_payload(caller_id, submission), version
        )
        decision = classify_idempotent_request(stored, candidate)
        if decision is IdempotencyDecision.CONFLICT:
            raise IdempotencyConflict("idempotency key bound to a different payload")
        if decision is not IdempotencyDecision.REPLAY:
            raise BindingIntegrityError("live binding classified as new")
        return stored, decision

    @staticmethod
    async def _live_binding(
        s: AsyncSession, caller_id: str, channel: Channel, key: str
    ) -> dict[str, Any] | None:
        res = await s.execute(
            select(messages).where(
                messages.c.caller_id == caller_id,
                messages.c.channel == channel.value,
                messages.c.idempotency_key == key,
                messages.c.binding_released_at.is_(None),
            )
        )
        r = res.mappings().first()
        return dict(r) if r is not None else None

    @staticmethod
    async def _get_row(
        s: AsyncSession, message_id: uuid.UUID, *, lock: bool = False
    ) -> dict[str, Any] | None:
        q = select(messages).where(messages.c.id == message_id)
        if lock:
            q = q.with_for_update()
        r = (await s.execute(q)).mappings().first()
        return dict(r) if r is not None else None

    async def get_for_caller(self, caller_id: str, message_id: uuid.UUID) -> StoredMessage | None:
        async with self._sf() as s:
            res = await s.execute(
                select(messages).where(
                    messages.c.id == message_id, messages.c.caller_id == caller_id
                )
            )
            row = res.mappings().first()
        return None if row is None else row_to_message(dict(row))

    async def cancel_for_caller(
        self, caller_id: str, message_id: uuid.UUID, now: datetime
    ) -> tuple[CancelOutcome, StoredMessage | None]:
        """Cancel the caller's own waiting Telegram message (contract 3.4.0).

        One conditional UPDATE: it matches only an ``accepted`` Telegram row of this
        caller that no worker holds (no lease, or the lease ran out, exactly as
        ``claim_due`` sees it) and that has not reached ``expires_at``. A claim locks the
        row and sets the lease in its own transaction, so whichever commits first wins:
        the loser's WHERE no longer matches (row lock, then re-check under READ
        COMMITTED). The content (text, chat id, buttons) is deleted in the same
        transaction. Nothing ever reached the provider.
        """
        async with self._sf() as s, s.begin():
            cancelled = (
                await s.execute(
                    update(messages)
                    .where(
                        messages.c.id == message_id,
                        messages.c.caller_id == caller_id,
                        messages.c.channel == Channel.TELEGRAM.value,
                        messages.c.status == MessageStatus.ACCEPTED.value,
                        or_(
                            messages.c.lease_expires_at.is_(None),
                            messages.c.lease_expires_at <= now,
                        ),
                        or_(messages.c.expires_at.is_(None), messages.c.expires_at > now),
                    )
                    .values(
                        status=MessageStatus.FAILED.value,
                        failure_code=FailureCode.CANCELLED.value,
                        content_purged_at=now,
                        updated_at=now,
                        not_before=None,
                        lease_token=None,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .returning(messages.c.id)
                )
            ).scalar_one_or_none()
            if cancelled is not None:
                await self._purge_content(s, message_id, now)
            row = (
                (
                    await s.execute(
                        select(messages).where(
                            messages.c.id == message_id, messages.c.caller_id == caller_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return CancelOutcome.NOT_FOUND, None
        stored = row_to_message(dict(row))
        if stored.channel is not Channel.TELEGRAM:
            return CancelOutcome.NOT_TELEGRAM, None
        if stored.status is MessageStatus.FAILED and stored.failure_code is FailureCode.CANCELLED:
            return CancelOutcome.CANCELLED, stored
        return CancelOutcome.NOT_CANCELLABLE, None

    # ------------------------------------------------------------- worker side

    async def _expire_due_accepted(self, s: AsyncSession, now: datetime, limit: int) -> int:
        ids = (
            select(messages.c.id)
            .where(
                messages.c.status == MessageStatus.ACCEPTED.value,
                messages.c.expires_at <= now,
                or_(messages.c.lease_expires_at.is_(None), messages.c.lease_expires_at <= now),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        expired = (
            (
                await s.execute(
                    update(messages)
                    .where(messages.c.id.in_(ids.scalar_subquery()))
                    .values(
                        status=MessageStatus.EXPIRED.value,
                        failure_code=FailureCode.EXPIRED_BEFORE_SEND.value,
                        content_purged_at=now,
                        updated_at=now,
                        lease_token=None,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                    .returning(messages.c.id)
                )
            )
            .scalars()
            .all()
        )
        if expired:
            await s.execute(
                delete(message_content).where(message_content.c.message_id.in_(list(expired)))
            )
        return len(expired)

    @staticmethod
    async def _claimable_rows(
        s: AsyncSession,
        base: Select[Any],
        now: datetime,
        limit: int,
        aged_after: timedelta | None,
    ) -> list[dict[str, Any]]:
        """Lock up to ``limit`` claimable rows in claim order (SKIP LOCKED).

        Without ageing: ``priority, created_at``. With ageing (SMS-03), in three passes
        that each follow an index: OTPs; then every other SMS accepted at least
        ``aged_after`` ago, oldest first, whatever its purpose, so an aged broadcast goes
        ahead of newer transactional and alert traffic but never ahead of older; then the
        rest by ``priority, created_at``. Telegram rows are never in the aged pass: they go
        strictly by ``priority, created_at``, so bot replies beat waiting broadcasts.
        """

        async def take(q: Select[Any], n: int, skip: list[uuid.UUID]) -> list[dict[str, Any]]:
            if skip:
                q = q.where(messages.c.id.not_in(skip))
            q = q.limit(n).with_for_update(skip_locked=True, of=messages)
            return [dict(r) for r in (await s.execute(q)).mappings().all()]

        by_priority = (messages.c.priority, messages.c.created_at, messages.c.id)
        if aged_after is None:
            return await take(base.order_by(*by_priority), limit, [])
        otp = PURPOSE_PRIORITY[Purpose.OTP]
        rows = await take(base.where(messages.c.priority == otp).order_by(*by_priority), limit, [])
        if len(rows) < limit:
            # SMS rows only (contract 3.4.0): on Telegram a bot reply (transactional) is
            # always claimed before waiting broadcasts, however long they have waited.
            aged = base.where(
                messages.c.priority > otp,
                messages.c.created_at <= now - aged_after,
                messages.c.channel == Channel.SMS.value,
            ).order_by(messages.c.created_at, messages.c.id)
            rows += await take(aged, limit - len(rows), [r["id"] for r in rows])
        if len(rows) < limit:
            rest = base.order_by(*by_priority)
            rows += await take(rest, limit - len(rows), [r["id"] for r in rows])
        return rows

    async def claim_due(
        self,
        worker_id: str,
        now: datetime,
        lease: timedelta,
        limit: int,
        *,
        channels: tuple[Channel, ...] | None = None,
        allow_broadcast: bool | Collection[Channel] = True,
        aged_after: timedelta | None = None,
    ) -> list[Claim]:
        """``allow_broadcast``: True (every channel), False (none) or, contract 3.4.0, the
        channels whose broadcasts may be claimed (Telegram-only switch)."""
        if lease <= timedelta(0) or limit <= 0:
            raise ValueError("lease and limit must be positive")
        if aged_after is not None and aged_after <= timedelta(0):
            raise ValueError("aged_after must be positive")
        claims: list[Claim] = []
        async with self._sf() as s, s.begin():
            await self._key_lock_shared(s)
            await self._expire_due_accepted(s, now, max(limit, 100))
            base = (
                select(
                    messages,
                    message_content.c.key_version.label("c_key_version"),
                    message_content.c.nonce,
                    message_content.c.ciphertext,
                )
                .join(message_content, message_content.c.message_id == messages.c.id)
                .join(callers, callers.c.caller_id == messages.c.caller_id)
                .where(
                    messages.c.status == MessageStatus.ACCEPTED.value,
                    or_(messages.c.not_before.is_(None), messages.c.not_before <= now),
                    or_(messages.c.expires_at.is_(None), messages.c.expires_at > now),
                    or_(messages.c.lease_expires_at.is_(None), messages.c.lease_expires_at <= now),
                    # Held, not failed, while unauthorized: disabled caller, removed
                    # channel/purpose, or a bot mapping that differs from the one
                    # authorized at acceptance. Expiry handling above still applies.
                    dispatch_authorized(),
                    ~select(dispatch_controls.c.channel)
                    .where(
                        dispatch_controls.c.channel == messages.c.channel,
                        or_(
                            dispatch_controls.c.suspended, dispatch_controls.c.cooldown_until > now
                        ),
                    )
                    .exists(),
                    _broadcast_filter(allow_broadcast),
                    messages.c.channel.in_([c.value for c in channels])
                    if channels is not None
                    else true(),
                )
            )
            rows = await self._claimable_rows(s, base, now, limit, aged_after)
            for row in rows:
                try:
                    content = self._cipher.decrypt(
                        row["id"],
                        row["channel"],
                        EncryptedContent(row["c_key_version"], row["nonce"], row["ciphertext"]),
                    )
                except ValueError:
                    # A key mismatch is not proof of corruption. Defer, retain content,
                    # and alert; never terminally fail/purge an undecryptable row.
                    await s.execute(
                        update(messages)
                        .where(messages.c.id == row["id"])
                        .values(not_before=now + timedelta(minutes=5), updated_at=now)
                    )
                    log.error("content unavailable; dispatch deferred message_id=%s", row["id"])
                    continue
                bot = row["authorized_bot"]
                token = uuid.uuid4()
                lease_until = now + lease
                await s.execute(
                    update(messages)
                    .where(messages.c.id == row["id"])
                    .values(lease_token=token, lease_owner=worker_id, lease_expires_at=lease_until)
                )
                row.update(lease_token=token, lease_owner=worker_id, lease_expires_at=lease_until)
                attempt = row["attempts"] + 1
                request: SendRequest
                if row["channel"] == Channel.SMS.value:
                    request = SmsSendRequest(
                        message_id=row["id"],
                        attempt=attempt,
                        recipient=content["recipient"],
                        text=content["text"],
                        expires_at=row["expires_at"],
                    )
                else:
                    request = TelegramSendRequest(
                        message_id=row["id"],
                        attempt=attempt,
                        bot=bot,
                        chat_id=content["chat_id"],
                        text=content["text"],
                        expires_at=row["expires_at"],
                        reply_markup=(
                            parse_reply_markup(content["reply_markup"])
                            if content.get("reply_markup") is not None
                            else None
                        ),
                    )
                claims.append(
                    Claim(
                        message=row_to_message(row),
                        claim_token=token,
                        lease_expires_at=lease_until,
                        send_request=request,
                    )
                )
        return claims

    async def _gate_and_lock_callers(
        self, s: AsyncSession, channel: Channel, caller_ids: Sequence[str], now: datetime
    ) -> None:
        """Check the provider scope and lock the caller rows (FOR SHARE), in that order."""
        # An absent row means the scope was never restricted (finish creates it).
        gate = (
            (
                await s.execute(
                    select(dispatch_controls)
                    .where(dispatch_controls.c.channel == channel.value)
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .first()
        )
        if gate is not None and (
            gate["suspended"] or (gate["cooldown_until"] and gate["cooldown_until"] > now)
        ):
            raise DispatchNotAuthorized("provider scope unavailable")
        await s.execute(
            select(callers.c.caller_id)
            .where(callers.c.caller_id.in_(sorted(set(caller_ids))))
            .order_by(callers.c.caller_id)
            .with_for_update(read=True)
        )

    async def _begin_row(
        self,
        s: AsyncSession,
        claim: Claim,
        at: datetime,
        units: int,
        reservation: Reservation | None,
    ) -> int | None:
        """``sending`` + attempt row in one statement; None when the update did not match."""
        mid = claim.message.id
        authorized = (
            select(callers.c.caller_id)
            .where(callers.c.caller_id == messages.c.caller_id, dispatch_authorized())
            .exists()
        )
        began = (
            update(messages)
            .where(
                messages.c.id == mid,
                messages.c.lease_token == claim.claim_token,
                messages.c.status == MessageStatus.ACCEPTED.value,
                messages.c.lease_expires_at > at,
                or_(messages.c.expires_at.is_(None), messages.c.expires_at > at),
                authorized,
            )
            .values(
                status=MessageStatus.SENDING.value,
                attempts=messages.c.attempts + 1,
                last_attempt_at=at,
                updated_at=at,
            )
            .returning(messages.c.attempts, messages.c.lease_owner)
            .cte("began")
        )
        tz = message_attempts.c.started_at.type
        # One round trip: state change and attempt row together (the attempt row
        # exists exactly when the update matched).
        insert_attempt = (
            message_attempts.insert()
            .from_select(
                [
                    "message_id",
                    "attempt_no",
                    "claim_token",
                    "worker_id",
                    "started_at",
                    "units",
                    "pace_total_until",
                    "pace_bulk_until",
                ],
                select(
                    literal(mid, message_attempts.c.message_id.type),
                    began.c.attempts,
                    literal(claim.claim_token, message_attempts.c.claim_token.type),
                    began.c.lease_owner,
                    literal(at, tz),
                    literal(units, message_attempts.c.units.type),
                    literal(None if reservation is None else reservation.pace_total_until, tz),
                    literal(None if reservation is None else reservation.pace_bulk_until, tz),
                ),
            )
            .add_cte(began)
            .returning(message_attempts.c.attempt_no)
        )
        got = (await s.execute(insert_attempt)).first()
        return None if got is None else int(got[0])

    async def _not_begun(self, s: AsyncSession, claim: Claim, at: datetime) -> Exception:
        """Why ``_begin_row`` did not match: ``LeaseLost`` or ``MessageExpired`` (nothing
        changed), or ``DispatchNotAuthorized`` after releasing the lease (the message is
        held: accepted and unclaimable, binding and content intact)."""
        mid = claim.message.id
        row = await self._get_row(s, mid, lock=True)
        if (
            row is None
            or row["lease_token"] != claim.claim_token
            or row["status"] != MessageStatus.ACCEPTED.value
        ):
            return LeaseLost("claim no longer owns the message")
        if row["expires_at"] is not None and row["expires_at"] <= at:
            return MessageExpired("message expired before the provider call")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= at:
            return LeaseLost("claim lease expired")
        await s.execute(
            update(messages)
            .where(messages.c.id == mid, messages.c.lease_token == claim.claim_token)
            .values(lease_token=None, lease_owner=None, lease_expires_at=None)
        )
        log.info("dispatch held: caller not authorized (message_id=%s)", mid)
        return DispatchNotAuthorized("caller is not authorized to dispatch this message")

    @staticmethod
    def _reservation(value: datetime | Reservation | None) -> Reservation | None:
        if value is None:
            return None
        return value if isinstance(value, Reservation) else Reservation(at=value)

    async def begin_attempt(
        self,
        claim: Claim,
        now: datetime,
        *,
        reserve: ReserveHook | None = None,
        progress: BeginProgress | None = None,
        units: int | None = None,
    ) -> int:
        """The dispatch authorization boundary. In one transaction: check the provider
        scope, lock the caller row (FOR SHARE, so a concurrent permission change waits until
        this commits), re-check lease, expiry and authorization, then commit ``sending`` +
        attempt row. Revocation after this commit cannot recall the provider call.

        ``reserve`` (optional) is the provider rate reservation (MSG-07b, F-03). It runs in
        THIS transaction, after the scope and caller locks and immediately before the state
        change. It returns the shared effective time (or a ``Reservation`` carrying it and
        the pace ledger), used for the lease and expiry checks and as ``started_at``. If it
        raises, the transaction rolls back and nothing began. ``progress.commit_sent``
        becomes true immediately before COMMIT is issued, so an interruption before that
        point provably committed nothing. ``units`` defaults to the SMS parts of the text.

        Raises ``LeaseLost``, ``MessageExpired`` or ``DispatchNotAuthorized``; the last
        releases the lease and leaves the message accepted (held, binding and content kept).
        """
        held: Exception | None = None
        attempt = 0
        used = attempt_units(claim.send_request) if units is None else units
        async with self._sf() as s, s.begin():
            await self._gate_and_lock_callers(
                s, claim.message.channel, [claim.message.caller_id], now
            )
            reservation = None
            if reserve is not None:
                reservation = self._reservation(await reserve(await s.connection()))
            at = now if reservation is None else reservation.at
            got = await self._begin_row(s, claim, at, used, reservation)
            if got is None:
                reason = await self._not_begun(s, claim, at)
                if not isinstance(reason, DispatchNotAuthorized):
                    raise reason  # rolls back: nothing began
                held = reason  # the lease release commits
            else:
                attempt = got
                if progress is not None:
                    progress.commit_sent = True  # COMMIT follows on block exit
        if held is not None:
            raise held
        return attempt

    async def begin_batch(
        self,
        claims: Sequence[Claim],
        now: datetime,
        *,
        reserve: ReserveHook | None = None,
        progress: BeginProgress | None = None,
    ) -> list[int | Exception]:
        """``begin_attempt`` for several claims of one channel that go out in ONE provider
        request (8028 batching). One transaction: scope check, caller locks, ONE rate
        reservation for the whole request, then ``sending`` + attempt row per claim.

        Returns one entry per claim, in order: the attempt number, or the reason it did not
        begin (``LeaseLost``, ``MessageExpired``, or ``DispatchNotAuthorized`` with the
        lease released). One claim that does not begin never stops the others. A scope that
        is unavailable raises ``DispatchNotAuthorized`` for the whole batch; a ``reserve``
        that raises rolls everything back.
        """
        if not claims:
            raise ValueError("begin_batch needs at least one claim")
        channel = claims[0].message.channel
        if any(c.message.channel is not channel for c in claims):
            raise ValueError("a batch is one channel")
        outcomes: list[int | Exception] = []
        async with self._sf() as s, s.begin():
            await self._gate_and_lock_callers(
                s, channel, [c.message.caller_id for c in claims], now
            )
            reservation = None
            if reserve is not None:
                reservation = self._reservation(await reserve(await s.connection()))
            at = now if reservation is None else reservation.at
            for claim in claims:
                got = await self._begin_row(
                    s, claim, at, attempt_units(claim.send_request), reservation
                )
                outcomes.append(got if got is not None else await self._not_begun(s, claim, at))
            if progress is not None and any(isinstance(o, int) for o in outcomes):
                progress.commit_sent = True  # COMMIT follows on block exit
        return outcomes

    async def provider_attempt_count(self, message_id: uuid.UUID) -> int:
        """Exclude durable local start failures from the provider retry budget."""
        async with self._sf() as s:
            return int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(message_attempts)
                        .where(
                            message_attempts.c.message_id == message_id,
                            or_(
                                message_attempts.c.provider_status.is_(None),
                                message_attempts.c.provider_status.not_in(
                                    ["local_start_late", "expired"]
                                ),
                            ),
                        )
                    )
                ).scalar_one()
            )

    async def finish_attempt(
        self, claim: Claim, result: ProviderResult, next_not_before: datetime | None
    ) -> StoredMessage:
        mid = claim.message.id
        now = result.finished_at
        async with self._sf() as s, s.begin():
            await self._key_lock_shared(s)
            if (
                result.outcome is ProviderOutcome.SENT
                and result.provider_message_id
                and isinstance(claim.send_request, SmsSendRequest)
            ):
                # Taken before the row lock (global order) and held until commit: a
                # concurrent callback either commits its orphan before our scan, or waits
                # and then sees our committed provider_message_id.
                await self._receipt_lock(s, result.provider_message_id)
            await s.execute(
                pg_insert(dispatch_controls)
                .values(channel=claim.message.channel.value, updated_at=now)
                .on_conflict_do_nothing(index_elements=[dispatch_controls.c.channel])
            )
            gate = (
                (
                    await s.execute(
                        select(dispatch_controls)
                        .where(dispatch_controls.c.channel == claim.message.channel.value)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            row = await self._get_row(s, mid, lock=True)
            if row is None:
                raise LeaseLost("message no longer exists")
            evidence = (
                (
                    await s.execute(
                        select(message_attempts).where(
                            message_attempts.c.message_id == mid,
                            message_attempts.c.attempt_no == claim.send_request.attempt,
                            message_attempts.c.claim_token == claim.claim_token,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if evidence is None or row["attempts"] != claim.send_request.attempt:
                raise LeaseLost("attempt fence changed")
            if evidence["outcome"] == result.outcome.value and evidence["finished_at"] is not None:
                if (
                    evidence["provider_message_id"] == result.provider_message_id
                    and evidence["provider_status"] == result.provider_status
                ):
                    # A lost COMMIT acknowledgement: return current state, including
                    # any stronger receipt evidence; never downgrade or send again.
                    return row_to_message(row)
            retry_after = (
                None
                if result.retry_after_seconds is None
                else min(result.retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
            )
            late_success = (
                row["status"] == MessageStatus.UNKNOWN.value
                and row["provider_status"] == "interrupted"
                and evidence["provider_status"] == "interrupted"
                and result.outcome is ProviderOutcome.SENT
            )
            if not late_success and (
                row["lease_token"] != claim.claim_token
                or row["status"] != MessageStatus.SENDING.value
            ):
                raise LeaseLost("claim no longer owns the message")
            # Failure outcomes affect the exact message as before; protect the rest
            # of the backlog. A late success never automatically resumes a scope.
            if gate is not None:
                changes: dict[str, Any] = {"updated_at": now}
                if result.failure_code in (FailureCode.PROVIDER_AUTH, FailureCode.PROVIDER_CONFIG):
                    changes["suspended"] = True
                elif result.outcome is ProviderOutcome.UNKNOWN:
                    count = gate["ambiguous_count"] + 1
                    changes["ambiguous_count"] = count
                    if count >= 3:
                        changes["cooldown_until"] = now + timedelta(seconds=60)
                        changes["ambiguous_count"] = 0
                elif result.outcome is ProviderOutcome.SENT:
                    changes["ambiguous_count"] = 0
                if retry_after is not None:
                    changes["cooldown_until"] = max(
                        gate["cooldown_until"] or now,
                        now + timedelta(seconds=retry_after),
                    )
                await s.execute(
                    update(dispatch_controls)
                    .where(dispatch_controls.c.channel == claim.message.channel.value)
                    .values(**changes)
                )
            current = MessageStatus(row["status"])
            values: dict[str, Any] = {
                "provider_status": result.provider_status,
                "updated_at": now,
                "lease_token": None,
                "lease_owner": None,
                "lease_expires_at": None,
            }
            if result.outcome is ProviderOutcome.SENT:
                new = MessageStatus.SENT
                values.update(
                    sent_at=now, provider_message_id=result.provider_message_id, failure_code=None
                )
            elif result.outcome is ProviderOutcome.REJECTED:
                new = MessageStatus.FAILED
                values.update(
                    failure_code=result.failure_code.value
                    if result.failure_code
                    else FailureCode.PROVIDER_REJECTED.value
                )
            elif result.outcome is ProviderOutcome.UNKNOWN:
                new = MessageStatus.UNKNOWN
                values.update(
                    failure_code=result.failure_code.value
                    if result.failure_code
                    else FailureCode.INTERRUPTED.value
                )
            else:  # NOT_SENT: provably never reached the provider
                retry_at = max(now, next_not_before or now)
                if retry_after is not None:
                    # Never shorten the provider's required wait (bounded above).
                    retry_at = max(retry_at, now + timedelta(seconds=retry_after))
                expires = row["expires_at"]
                if result.failure_code is FailureCode.EXPIRED_BEFORE_SEND or (
                    result.retryable and expires is not None and retry_at >= expires
                ):
                    new = MessageStatus.EXPIRED
                    values.update(failure_code=FailureCode.EXPIRED_BEFORE_SEND.value)
                elif result.retryable:
                    new = MessageStatus.ACCEPTED
                    values.update(not_before=retry_at, failure_code=None)
                else:
                    new = MessageStatus.FAILED
                    values.update(
                        failure_code=(result.failure_code or FailureCode.PROVIDER_CONFIG).value
                    )
            if new not in ALLOWED_TRANSITIONS[current]:
                raise BindingIntegrityError("illegal status transition")
            values["status"] = new.value
            await s.execute(update(messages).where(messages.c.id == mid).values(**values))
            await s.execute(
                update(message_attempts)
                .where(
                    message_attempts.c.message_id == mid,
                    message_attempts.c.attempt_no == row["attempts"],
                    message_attempts.c.claim_token == claim.claim_token,
                )
                .values(
                    finished_at=now,
                    outcome=result.outcome.value,
                    provider_status=result.provider_status,
                    failure_code=result.failure_code.value if result.failure_code else None,
                    provider_message_id=result.provider_message_id,
                    retryable=result.retryable,
                    retry_after_seconds=retry_after,
                    detail=result.detail,
                )
            )
            if (
                new is MessageStatus.SENT
                and isinstance(claim.send_request, SmsSendRequest)
                and result.provider_message_id
            ):
                await self._rematch_orphans(
                    s, mid, result.provider_message_id, claim.send_request.recipient, now
                )
            if new in PURGEABLE_STATUSES:
                await self._purge_content(s, mid, now)
            final = await self._get_row(s, mid)
            assert final is not None
            stored = row_to_message(final)
        return stored

    async def mark_expired(self, claim: Claim, now: datetime) -> StoredMessage:
        mid = claim.message.id
        async with self._sf() as s, s.begin():
            res = await s.execute(
                update(messages)
                .where(
                    messages.c.id == mid,
                    messages.c.lease_token == claim.claim_token,
                    messages.c.status == MessageStatus.ACCEPTED.value,
                )
                .values(
                    status=MessageStatus.EXPIRED.value,
                    failure_code=FailureCode.EXPIRED_BEFORE_SEND.value,
                    updated_at=now,
                    lease_token=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .returning(messages.c.id)
            )
            if res.scalar_one_or_none() is None:
                raise LeaseLost("claim no longer owns the message")
            await self._purge_content(s, mid, now)
            row = await self._get_row(s, mid)
            assert row is not None
            stored = row_to_message(row)
        return stored

    async def release_claim(self, claim: Claim, now: datetime) -> bool:
        """Give back a claim that never began an attempt (provably unsent).

        Clears the lease only while it is still ours and the message is still
        ``accepted``, so the message is claimable again at once, in its normal
        priority order, instead of after the lease lapses. Returns False when the
        claim no longer owns the message; that is harmless.
        """
        async with self._sf() as s, s.begin():
            res = await s.execute(
                update(messages)
                .where(
                    messages.c.id == claim.message.id,
                    messages.c.lease_token == claim.claim_token,
                    messages.c.status == MessageStatus.ACCEPTED.value,
                )
                .values(lease_token=None, lease_owner=None, lease_expires_at=None, updated_at=now)
                .returning(messages.c.id)
            )
            return res.scalar_one_or_none() is not None

    async def recover_stale(self, now: datetime) -> int:
        async with self._sf() as s, s.begin():
            stale = (
                select(messages.c.id)
                .where(
                    messages.c.status == MessageStatus.SENDING.value,
                    messages.c.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
            ids = (
                (
                    await s.execute(
                        update(messages)
                        .where(messages.c.id.in_(stale.scalar_subquery()))
                        .values(
                            status=MessageStatus.UNKNOWN.value,
                            failure_code=FailureCode.INTERRUPTED.value,
                            provider_status="interrupted",
                            updated_at=now,
                            content_purged_at=now,
                            lease_token=None,
                            lease_owner=None,
                            lease_expires_at=None,
                        )
                        .returning(messages.c.id)
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                await s.execute(
                    delete(message_content).where(message_content.c.message_id.in_(list(ids)))
                )
                await s.execute(
                    update(message_attempts)
                    .where(
                        message_attempts.c.message_id.in_(list(ids)),
                        message_attempts.c.finished_at.is_(None),
                    )
                    .values(
                        finished_at=now,
                        outcome=ProviderOutcome.UNKNOWN.value,
                        failure_code=FailureCode.INTERRUPTED.value,
                        provider_status="interrupted",
                    )
                )
            # Abandoned claims that never began an attempt become claimable again.
            await s.execute(
                update(messages)
                .where(
                    messages.c.status == MessageStatus.ACCEPTED.value,
                    messages.c.lease_expires_at <= now,
                )
                .values(lease_token=None, lease_owner=None, lease_expires_at=None)
            )
        return len(ids)

    # ------------------------------------------------------------------ receipts

    async def record_receipt(self, receipt: DeliveryReceipt, now: datetime) -> ReceiptDisposition:
        masked = mask_recipient(receipt.recipient)
        async with self._sf() as s, s.begin():
            await self._key_lock_shared(s)
            await self._receipt_lock(s, receipt.provider_message_id)
            found = (
                (
                    await s.execute(
                        select(messages)
                        .where(
                            messages.c.channel == Channel.SMS.value,
                            messages.c.provider_message_id == receipt.provider_message_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            message_id: uuid.UUID | None = None
            if found is None:
                version = self._fp.current_version
                disposition = ReceiptDisposition.ORPHANED
            else:
                row = dict(found)
                message_id = row["id"]
                stored = row_to_message(row)
                version = row["recipient_fp_version"] or self._fp.current_version
                deadline = recipient_match_deadline(stored, self._policy)
                if stored.recipient_fingerprint is None or (
                    deadline is not None and now > deadline
                ):
                    disposition = ReceiptDisposition.UNMATCHED_LATE
                else:
                    await self._ensure_not_retired(s, "fingerprint", [version])
                    candidate = self._fingerprint(
                        FingerprintDomain.RECIPIENT, canonical_recipient(receipt.recipient), version
                    )
                    if not stored.recipient_fingerprint.matches(candidate):
                        disposition = ReceiptDisposition.MISMATCHED
                        log.warning("receipt recipient mismatch (message_id=%s)", message_id)
                    else:
                        disposition = await self._apply_receipt(
                            s, row, receipt.raw_status, receipt.event_time, now
                        )
            await self._ensure_not_retired(s, "fingerprint", [version])
            fp = self._fingerprint(
                FingerprintDomain.RECIPIENT, canonical_recipient(receipt.recipient), version
            )
            await s.execute(
                receipts.insert().values(
                    id=uuid.uuid4(),
                    provider=Provider.SMS_8028.value,
                    provider_message_id=receipt.provider_message_id,
                    recipient_fp_version=fp.key_version,
                    recipient_fp_digest=fp.digest,
                    recipient_masked=masked,
                    raw_status=receipt.raw_status,
                    event_time=receipt.event_time,
                    received_at=receipt.received_at,
                    disposition=disposition.value,
                    message_id=message_id,
                )
            )
        return disposition

    async def _apply_receipt(
        self,
        s: AsyncSession,
        row: dict[str, Any],
        raw_status: str,
        event_time: datetime,
        now: datetime,
    ) -> ReceiptDisposition:
        try:
            mapped = RECEIPT_STATUS_MAP[ReceiptStatus(raw_status)]
        except ValueError:
            log.info(
                "undocumented receipt status recorded without change (message_id=%s)", row["id"]
            )
            return ReceiptDisposition.RECORDED_NO_CHANGE
        target, failure, _final = mapped
        current = MessageStatus(row["status"])
        if target is current or target not in ALLOWED_TRANSITIONS[current]:
            return ReceiptDisposition.RECORDED_NO_CHANGE
        values: dict[str, Any] = {
            "status": target.value,
            "provider_status": raw_status,
            "updated_at": now,
        }
        if target is MessageStatus.DELIVERED:
            values["delivered_at"] = event_time
        if target is MessageStatus.SENT and row["sent_at"] is None:
            values["sent_at"] = event_time
        if failure is not None:
            values["failure_code"] = failure.value
        elif target in (MessageStatus.SENT, MessageStatus.DELIVERED):
            values["failure_code"] = None
        await s.execute(update(messages).where(messages.c.id == row["id"]).values(**values))
        row.update(values)
        return ReceiptDisposition.APPLIED

    async def _rematch_orphans(
        self,
        s: AsyncSession,
        message_id: uuid.UUID,
        provider_message_id: str,
        recipient: str,
        now: datetime,
    ) -> None:
        orphans = (
            (
                await s.execute(
                    select(receipts)
                    .where(
                        receipts.c.provider == Provider.SMS_8028.value,
                        receipts.c.provider_message_id == provider_message_id,
                        receipts.c.disposition == ReceiptDisposition.ORPHANED.value,
                    )
                    .order_by(receipts.c.event_time, receipts.c.received_at)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if not orphans:
            return
        row = await self._get_row(s, message_id)
        assert row is not None
        for orphan in orphans:
            version = orphan["recipient_fp_version"]
            await self._ensure_not_retired(s, "fingerprint", [version])
            candidate = self._fingerprint(
                FingerprintDomain.RECIPIENT, canonical_recipient(recipient), version
            )
            stored_fp = KeyedFingerprint(
                key_version=version,
                domain=FingerprintDomain.RECIPIENT,
                digest=orphan["recipient_fp_digest"],
            )
            if not stored_fp.matches(candidate):
                disposition = ReceiptDisposition.MISMATCHED
                log.warning("orphan receipt recipient mismatch (message_id=%s)", message_id)
            else:
                disposition = await self._apply_receipt(
                    s, row, orphan["raw_status"], orphan["event_time"], now
                )
            await s.execute(
                update(receipts)
                .where(receipts.c.id == orphan["id"])
                .values(disposition=disposition.value, message_id=message_id)
            )

    # ------------------------------------------------------------------- cleanup

    @staticmethod
    async def _purge_content(s: AsyncSession, message_id: uuid.UUID, now: datetime) -> None:
        await s.execute(delete(message_content).where(message_content.c.message_id == message_id))
        await s.execute(
            update(messages)
            .where(messages.c.id == message_id, messages.c.content_purged_at.is_(None))
            .values(content_purged_at=now)
        )

    async def purge_due(self, now: datetime, limit: int) -> int:
        """Crash-safe backstop. Each step is bounded by ``limit`` and idempotent."""
        p = self._policy
        touched = 0
        async with self._sf() as s, s.begin():
            # 1. Content of purgeable messages that still has it (crash between steps).
            stale_content = (
                select(message_content.c.message_id)
                .join(messages, messages.c.id == message_content.c.message_id)
                .where(messages.c.status.in_(_PURGEABLE))
                .limit(limit)
                .with_for_update(of=message_content, skip_locked=True)
            )
            ids = (
                (
                    await s.execute(
                        delete(message_content)
                        .where(message_content.c.message_id.in_(stale_content.scalar_subquery()))
                        .returning(message_content.c.message_id)
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                await s.execute(
                    update(messages)
                    .where(messages.c.id.in_(list(ids)), messages.c.content_purged_at.is_(None))
                    .values(content_purged_at=now)
                )
            touched += len(ids)
            # Purgeable rows already without content but never stamped.
            stamped = await s.execute(
                update(messages)
                .where(
                    messages.c.status.in_(_PURGEABLE),
                    messages.c.content_purged_at.is_(None),
                    ~select(message_content.c.message_id)
                    .where(message_content.c.message_id == messages.c.id)
                    .exists(),
                )
                .values(content_purged_at=now)
            )
            touched += rowcount(stamped)

            # 2. Recipient fingerprints past recipient_match_deadline (anchor + window).
            anchor = func.coalesce(
                messages.c.sent_at, messages.c.last_attempt_at, messages.c.created_at
            )
            rfp = (
                select(messages.c.id)
                .where(
                    messages.c.channel == Channel.SMS.value,
                    messages.c.recipient_fp_digest.is_not(None),
                    messages.c.status.not_in(_CONTENT_REQUIRED),
                    anchor <= now - p.receipt_window,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            res = await s.execute(
                update(messages)
                .where(messages.c.id.in_(rfp.scalar_subquery()))
                .values(recipient_fp_version=None, recipient_fp_digest=None)
            )
            touched += rowcount(res)

            # 3. Completed bindings past the guarantee window (never outstanding ones).
            rel = (
                select(messages.c.id)
                .where(
                    messages.c.binding_released_at.is_(None),
                    messages.c.status.in_(_COMPLETED),
                    messages.c.created_at <= now - p.dedup_window,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            res = await s.execute(
                update(messages)
                .where(
                    messages.c.id.in_(rel.scalar_subquery()),
                    messages.c.status.in_(_COMPLETED),  # re-checked under the row lock
                )
                .values(binding_released_at=now, payload_fp_version=None, payload_fp_digest=None)
            )
            touched += rowcount(res)

            # 4. Receipt evidence past retention.
            old = (
                select(receipts.c.id)
                .where(
                    receipts.c.received_at <= now - p.receipt_evidence_retention,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            res = await s.execute(delete(receipts).where(receipts.c.id.in_(old.scalar_subquery())))
            touched += rowcount(res)
        return touched

    # ------------------------------------------------------------ key management

    async def fingerprint_key_versions_in_use(self, now: datetime) -> frozenset[str]:
        async with self._sf() as s:
            return await self._fp_versions_in_use(s)

    @staticmethod
    async def _fp_versions_in_use(s: AsyncSession) -> frozenset[str]:
        q = union(
            select(messages.c.payload_fp_version.label("v")).where(
                messages.c.binding_released_at.is_(None), messages.c.payload_fp_version.is_not(None)
            ),
            select(messages.c.recipient_fp_version.label("v")).where(
                messages.c.recipient_fp_version.is_not(None)
            ),
            select(receipts.c.recipient_fp_version.label("v")),
        )
        return frozenset((await s.execute(q)).scalars().all())

    async def content_key_versions_in_use(self) -> frozenset[str]:
        async with self._sf() as s:
            res = await s.execute(select(message_content.c.key_version).distinct())
            return frozenset(res.scalars().all())

    async def retire_key_version(self, kind: str, version: str, now: datetime) -> None:
        """Record retirement under an exclusive lock; refused while anything needs it.

        After this commits, no transaction can start using ``version`` (writers check
        under the shared lock), so the key material may then be removed from config.
        """
        if kind not in ("fingerprint", "content"):
            raise ValueError("unknown key kind")
        current = (
            self._fp.current_version if kind == "fingerprint" else self._cipher.current_version
        )
        async with self._sf() as s, s.begin():
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": KEY_LOCK_ID})
            if kind == "fingerprint":
                in_use = await self._fp_versions_in_use(s)
            else:
                res = await s.execute(select(message_content.c.key_version).distinct())
                in_use = frozenset(res.scalars().all())
            if not can_retire_key_version(version, current, in_use):
                raise KeyRetirementRefused("key version is current or still referenced")
            await s.execute(
                pg_insert(retired_key_versions)
                .values(kind=kind, key_version=version, retired_at=now)
                .on_conflict_do_nothing()
            )
