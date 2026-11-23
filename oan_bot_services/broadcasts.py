"""``POST /broadcasts`` and its reads (the DA kit's ``bot-services-broadcasts.openapi.yaml``)
and the jobs that send them through OAN Messaging (``oan-messaging-caller.md``).

* Idempotency: the ``Idempotency-Key`` is bound by keyed HMAC fingerprint to a body
  fingerprint for at least 30 days. Same key and body: the original answer (200). Same key,
  other body: 409 ``idempotency_conflict``.
* Recipients: linked farmers with updates on and a reachable chat, on their registry
  area (§6.8.1); or the R1 roster of every kebele in the scope, linked agents only, minus
  the sender (``not_on_telegram`` lists the others).
* Order: round-robin across woredas (TG-35); ``n`` is the position, and
  ``source_reference`` is ``bc:<broadcast_id>:<n>``.
* One OAN Messaging submit per recipient with a fresh UUID4 ``Idempotency-Key`` stored
  before the call; the chat id is cleared once OAN Messaging has the message.
* Results are polled at most once every 10 seconds per broadcast. Telegram never reports
  delivery, so ``sent`` is final for Bot Services.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from oan_bot_services import logs
from oan_bot_services.areas import kebeles_under
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.clients.oan_messaging import Rejected
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    broadcast_bindings,
    broadcast_recipients,
    broadcasts,
    subscriptions,
)

log = logs.get(__name__)

TELEGRAM_MAX_UTF16 = 4096
MAX_EXPIRY = timedelta(days=7)
PAGE_SIZE = 500
#: SMS-06: the DA app's source -> OAN Messaging purpose.
PURPOSE = {"Emergency": "alert", "Weather": "alert", "Knowledge": "broadcast",
           "Informational": "broadcast"}
#: Statuses Bot Services keeps; ``pending`` = not yet submitted (shown as ``accepted``).
OPEN = ("pending", "accepted", "sending")
COUNTED = ("accepted", "sending", "sent", "failed", "expired", "unknown")
#: OAN Messaging failure codes that mean the chat cannot be reached (TG-09), with the R3
#: reason. Values are ``FailureCode`` in ``oan_messaging/contracts.py``.
UNREACHABLE = {"recipient_blocked_bot": "blocked", "chat_not_found": "chat_deleted"}


class Problem(Exception):
    def __init__(self, status: int, code: str, errors: list[dict[str, str]] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status, self.code = status, code
        self.errors = errors or []
        self.headers = headers


def map_result(status: str, failure_code: str | None) -> tuple[str, bool]:
    """OAN Messaging message status -> (Bot Services status, unreachable?).

    ``sent`` and ``delivered`` count as ``sent`` (Telegram ends at ``sent``); ``failed``
    with a blocked or missing chat marks the recipient unreachable (TG-09); ``expired`` and
    ``unknown`` pass through (never resent; they get the SMS copy)."""
    if status in ("sent", "delivered"):
        return "sent", False
    if status == "failed":
        return "failed", failure_code in UNREACHABLE
    if status in ("expired", "unknown", "accepted", "sending"):
        return status, False
    return "unknown", False


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _has_control(text: str) -> bool:
    return any((ord(c) < 32 and c not in "\n\r") or ord(c) == 127 for c in text)


def compose(title: str, body: str) -> str:
    """TG-34: the title on its own first line, a blank line, then the message."""
    return f"{title}\n\n{body}"


def validate(req: dict[str, Any], now: datetime) -> tuple[str, datetime]:
    """Rules the schema cannot express. Returns (text, expires_at)."""
    errors: list[dict[str, str]] = []
    for name in ("title", "body"):
        if _has_control(req[name]):
            errors.append({"field": name, "code": "control_characters"})
    text = compose(req["title"], req["body"])
    if not req["title"].strip() and not req["body"].strip():
        errors.append({"field": "body", "code": "blank"})
    if utf16_units(text) > TELEGRAM_MAX_UTF16:
        errors.append({"field": "body", "code": "too_long"})
    expires_at: datetime | None = req.get("expires_at")
    if expires_at is not None:
        if expires_at <= now:
            errors.append({"field": "expires_at", "code": "in_past"})
        elif expires_at > now + MAX_EXPIRY:
            errors.append({"field": "expires_at", "code": "too_far"})
    if errors:
        raise Problem(422, "validation_failed", errors)
    return text, expires_at or now


async def _farmer_recipients(deps: Deps, level: str, code: str) -> list[dict[str, Any]]:
    column = {"region": subscriptions.c.region_code, "zone": subscriptions.c.zone_code,
              "woreda": subscriptions.c.woreda_code, "kebele": subscriptions.c.kebele_code}[level]
    async with deps.sf() as s:
        rows = (
            await s.execute(
                select(subscriptions.c.contact_ref, subscriptions.c.farmer_id,
                       subscriptions.c.chat_id, subscriptions.c.woreda_code)
                .where(column == code, subscriptions.c.updates_on,
                       subscriptions.c.state == "active")
                .order_by(subscriptions.c.woreda_code, subscriptions.c.contact_ref)
            )
        ).all()
    return [{"contact_ref": r.contact_ref, "farmer_id": r.farmer_id, "chat_id": r.chat_id,
             "woreda_code": r.woreda_code, "da_ref": None} for r in rows]


async def _da_recipients(
    deps: Deps, level: str, code: str, sender_ref: str
) -> tuple[list[dict[str, Any]], list[str]]:
    kebeles = await kebeles_under(deps, level, code)
    woreda_of = dict(kebeles)
    roster = await deps.da.roster([k for k, _ in kebeles]) if kebeles else []
    linked: OrderedDict[str, dict[str, Any]] = OrderedDict()
    not_linked: list[str] = []
    for entry in roster:
        for agent in entry.agents:
            if agent.ref == sender_ref or agent.ref in linked:
                continue
            if agent.telegram_linked and agent.telegram_id is not None:
                linked[agent.ref] = {
                    "contact_ref": None, "farmer_id": None, "da_ref": agent.ref,
                    "chat_id": agent.telegram_id,
                    "woreda_code": woreda_of.get(entry.kebele_code, entry.kebele_code),
                }
            elif agent.ref not in not_linked:
                not_linked.append(agent.ref)
    return list(linked.values()), [r for r in not_linked if r not in linked]


def round_robin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One from woreda A, one from B, ... (TG-35). Woredas in code order."""
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in sorted(rows, key=lambda r: r["woreda_code"]):
        groups.setdefault(row["woreda_code"], []).append(row)
    out: list[dict[str, Any]] = []
    queues = [list(g) for g in groups.values()]
    while queues:
        for q in queues:
            out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


def _created(row: Any) -> dict[str, Any]:
    return {"broadcast_id": str(row["broadcast_id"]), "recipients": row["recipients"],
            "not_on_telegram": list(row["not_on_telegram"])}


async def create(
    deps: Deps, key: str, raw: dict[str, Any], req: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """202 new, 200 replay; raises ``Problem``."""
    now = await deps.clock()
    key_fp = deps.keys.fingerprint("broadcast-key", key)
    body_fp = deps.keys.body_fingerprint(raw)
    replay = await _replay(deps, key_fp, body_fp)
    if replay is not None:
        return 200, replay
    text, expires_at = validate(req, now)
    if req.get("expires_at") is None:
        expires_at = now + timedelta(hours=deps.settings.default_broadcast_hours)
    level, code = req["scope"]["level"], req["scope"]["code"]
    try:
        if req["recipients"] == "farmers":
            rows = await _farmer_recipients(deps, level, code)
            not_on: list[str] = []
        else:
            rows, not_on = await _da_recipients(deps, level, code, req["sender_ref"])
    except Unavailable:
        raise Problem(503, "service_unavailable", headers={"Retry-After": "30"}) from None
    ordered = round_robin(rows)
    broadcast_id = uuid.uuid4()
    try:
        async with deps.sf() as s, s.begin():
            await s.execute(
                insert(broadcast_bindings).values(
                    key_fp=key_fp, body_fp=body_fp, broadcast_id=broadcast_id, created_at=now
                )
            )
            await s.execute(
                insert(broadcasts).values(
                    broadcast_id=broadcast_id, recipients_kind=req["recipients"],
                    scope_level=level, scope_code=code, sender_ref=req["sender_ref"],
                    sender_role=req["sender_role"], source=req["source"],
                    source_reference=req["source_reference"], purpose=PURPOSE[req["source"]],
                    text=deps.keys.seal(text, "broadcast") if ordered else None,
                    expires_at=expires_at, recipients=len(ordered), not_on_telegram=not_on,
                    state="running" if ordered else "finished", created_at=now,
                    finished_at=None if ordered else now, next_poll_at=now,
                )
            )
            if ordered:
                await s.execute(
                    insert(broadcast_recipients),
                    [
                        {"broadcast_id": broadcast_id, "n": n, "woreda_code": r["woreda_code"],
                         "contact_ref": r["contact_ref"], "farmer_id": r["farmer_id"],
                         "da_ref": r["da_ref"], "chat_id": r["chat_id"],
                         "idempotency_key": uuid.uuid4(), "status": "pending",
                         "updated_at": now}
                        for n, r in enumerate(ordered, start=1)
                    ],
                )
    except IntegrityError:
        # The same key arrived twice at once: answer as a replay of the winner.
        replay = await _replay(deps, key_fp, body_fp)
        if replay is None:
            raise Problem(503, "service_unavailable") from None
        return 200, replay
    log.info(
        "broadcast accepted broadcast_id=%s kind=%s level=%s recipients=%d not_on_telegram=%d",
        broadcast_id, req["recipients"], level, len(ordered), len(not_on),
    )
    return 202, {"broadcast_id": str(broadcast_id), "recipients": len(ordered),
                 "not_on_telegram": not_on}


async def _replay(deps: Deps, key_fp: str, body_fp: str) -> dict[str, Any] | None:
    async with deps.sf() as s:
        bound = (
            await s.execute(select(broadcast_bindings).where(broadcast_bindings.c.key_fp == key_fp))
        ).mappings().first()
        if bound is None:
            return None
        if not hmac.compare_digest(bound["body_fp"], body_fp):
            raise Problem(409, "idempotency_conflict",
                          [{"field": "header.Idempotency-Key", "code": "payload_mismatch"}])
        row = (
            await s.execute(
                select(broadcasts).where(broadcasts.c.broadcast_id == bound["broadcast_id"])
            )
        ).mappings().one()
    return _created(row)


def _parse_id(broadcast_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(broadcast_id)
    except ValueError:
        raise Problem(404, "not_found") from None


async def progress(deps: Deps, broadcast_id: str) -> dict[str, Any]:
    bid = _parse_id(broadcast_id)
    async with deps.sf() as s:
        state = (
            await s.execute(select(broadcasts.c.state).where(broadcasts.c.broadcast_id == bid))
        ).scalar_one_or_none()
        if state is None:
            raise Problem(404, "not_found")
        rows = (
            await s.execute(
                select(broadcast_recipients.c.status, func.count())
                .where(broadcast_recipients.c.broadcast_id == bid)
                .group_by(broadcast_recipients.c.status)
            )
        ).all()
    counts = dict.fromkeys(COUNTED, 0)
    for status, n in rows:
        counts["accepted" if status == "pending" else status] += int(n)
    return {"broadcast_id": str(bid), "state": state, "counts": counts}


def _cursor(n: int) -> str:
    return base64.urlsafe_b64encode(f"n:{n}".encode()).decode().rstrip("=")


def _from_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if not raw.startswith("n:"):
            raise ValueError
        return int(raw[2:])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise Problem(422, "validation_failed", [{"field": "cursor", "code": "invalid"}]) from None


async def telegram_recipients(deps: Deps, broadcast_id: str, cursor: str | None) -> dict[str, Any]:
    """Farmer IDs whose Telegram copy is ``sent``, in pages. The DA backend reads it when
    the broadcast is finished or after its ``sms_fallback_wait``; it reflects that moment."""
    bid = _parse_id(broadcast_id)
    after = _from_cursor(cursor)
    async with deps.sf() as s:
        exists = (
            await s.execute(
                select(broadcasts.c.broadcast_id).where(broadcasts.c.broadcast_id == bid)
            )
        ).first()
        if exists is None:
            raise Problem(404, "not_found")
        rows = (
            await s.execute(
                select(broadcast_recipients.c.n, broadcast_recipients.c.farmer_id)
                .where(broadcast_recipients.c.broadcast_id == bid,
                       broadcast_recipients.c.status == "sent",
                       broadcast_recipients.c.farmer_id.is_not(None),
                       broadcast_recipients.c.n > after)
                .order_by(broadcast_recipients.c.n)
                .limit(PAGE_SIZE + 1)
            )
        ).all()
    page = rows[:PAGE_SIZE]
    more = len(rows) > PAGE_SIZE
    return {"farmer_ids": [r.farmer_id for r in page],
            "next_cursor": _cursor(page[-1].n) if more else None}


# ------------------------------------------------------------------------- jobs


async def submit_pending(deps: Deps) -> int:
    """Submit up to ``batch_size`` pending recipients, oldest broadcast first, in order."""
    now = await deps.clock()
    submitted = 0
    async with deps.sf() as s:
        running = (
            await s.execute(
                select(broadcasts).where(broadcasts.c.state == "running",
                                         broadcasts.c.text.is_not(None))
                .order_by(broadcasts.c.created_at)
            )
        ).mappings().all()
    for b in running:
        async with deps.sf() as s:
            pending = (
                await s.execute(
                    select(broadcast_recipients)
                    .where(broadcast_recipients.c.broadcast_id == b["broadcast_id"],
                           broadcast_recipients.c.status == "pending")
                    .order_by(broadcast_recipients.c.n)
                    .limit(deps.settings.batch_size - submitted)
                )
            ).mappings().all()
        if not pending:
            await _clear_text(deps, b["broadcast_id"])
            continue
        text = deps.keys.open(b["text"], "broadcast")
        for r in pending:
            values: dict[str, Any]
            if b["expires_at"] <= now:
                values = {"status": "expired", "chat_id": None}
            else:
                try:
                    out = await deps.oan.submit(
                        idempotency_key=r["idempotency_key"], chat_id=int(r["chat_id"]),
                        text=text, purpose=b["purpose"], expires_at=b["expires_at"],
                        source_reference=f"bc:{b['broadcast_id']}:{r['n']}",
                    )
                except Unavailable:
                    log.warning("broadcast submit paused broadcast_id=%s", b["broadcast_id"])
                    return submitted
                if isinstance(out, Rejected):
                    log.warning("broadcast recipient refused broadcast_id=%s n=%d code=%s",
                                b["broadcast_id"], r["n"], out.code)
                    values = {"status": "failed", "failure_code": out.code, "chat_id": None}
                else:
                    status, _ = map_result(out.status, None)
                    values = {"status": status, "oan_message_id": out.message_id,
                              "chat_id": None}
            async with deps.sf() as s, s.begin():
                await s.execute(
                    update(broadcast_recipients)
                    .where(broadcast_recipients.c.broadcast_id == r["broadcast_id"],
                           broadcast_recipients.c.n == r["n"])
                    .values(updated_at=now, **values)
                )
            submitted += 1
        if submitted >= deps.settings.batch_size:
            break
    return submitted


async def _clear_text(deps: Deps, broadcast_id: uuid.UUID) -> None:
    async with deps.sf() as s, s.begin():
        await s.execute(
            update(broadcasts).where(broadcasts.c.broadcast_id == broadcast_id).values(text=None)
        )


async def poll_results(deps: Deps) -> int:
    """Read results for recipients not yet final, at most once every ``poll_interval``
    seconds per broadcast; finish a broadcast when nothing is open."""
    now = await deps.clock()
    interval = timedelta(seconds=deps.settings.poll_interval_seconds)
    polled = 0
    async with deps.sf() as s:
        due = (
            await s.execute(
                select(broadcasts.c.broadcast_id).where(
                    broadcasts.c.state == "running", broadcasts.c.next_poll_at <= now
                ).order_by(broadcasts.c.next_poll_at)
            )
        ).scalars().all()
    for bid in due:
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(broadcasts).where(broadcasts.c.broadcast_id == bid)
                .values(next_poll_at=now + interval)
            )
        async with deps.sf() as s:
            rows = (
                await s.execute(
                    select(broadcast_recipients.c.n, broadcast_recipients.c.oan_message_id)
                    .where(broadcast_recipients.c.broadcast_id == bid,
                           broadcast_recipients.c.status.in_(("accepted", "sending")),
                           broadcast_recipients.c.oan_message_id.is_not(None))
                    .order_by(broadcast_recipients.c.n)
                    .limit(deps.settings.batch_size)
                )
            ).all()
        for n, message_id in rows:
            try:
                state = await deps.oan.message(message_id)
            except Unavailable:
                return polled
            status, unreachable = ("unknown", False) if state is None else map_result(
                state.status, state.failure_code
            )
            if status in ("accepted", "sending"):
                if state is not None and status == "sending":
                    await _set(deps, bid, n, status="sending", updated_at=now)
                continue
            await _set(deps, bid, n, status=status, updated_at=now,
                       failure_code=None if state is None else state.failure_code)
            polled += 1
            if unreachable:
                await mark_unreachable(deps, bid, n)
        await _finish_if_done(deps, bid, now)
    return polled


async def _set(deps: Deps, bid: uuid.UUID, n: int, **values: Any) -> None:
    async with deps.sf() as s, s.begin():
        await s.execute(
            update(broadcast_recipients)
            .where(broadcast_recipients.c.broadcast_id == bid, broadcast_recipients.c.n == n)
            .values(**values)
        )


async def mark_unreachable(deps: Deps, bid: uuid.UUID, n: int) -> bool:
    """TG-09: a farmer's subscription is marked unreachable (later broadcasts skip it); a
    staff member is reported through R3. True when handled."""
    async with deps.sf() as s:
        r = (
            await s.execute(
                select(broadcast_recipients).where(
                    broadcast_recipients.c.broadcast_id == bid, broadcast_recipients.c.n == n
                )
            )
        ).mappings().one()
    reason = UNREACHABLE.get(r["failure_code"] or "", "blocked")
    if r["contact_ref"] is not None:
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(subscriptions).where(subscriptions.c.contact_ref == r["contact_ref"])
                .values(state="unreachable")
            )
        log.info("subscription unreachable contact_ref=%s", r["contact_ref"])
    elif r["da_ref"] is not None:
        try:
            await deps.da.report_unreachable(r["da_ref"], reason)  # type: ignore[arg-type]
        except Unavailable:
            log.warning("R3 deferred broadcast_id=%s n=%d", bid, n)
            return False
    await _set(deps, bid, n, unreachable_done=True)
    return True


async def retry_unreachable(deps: Deps) -> int:
    async with deps.sf() as s:
        rows = (
            await s.execute(
                select(broadcast_recipients.c.broadcast_id, broadcast_recipients.c.n).where(
                    broadcast_recipients.c.status == "failed",
                    broadcast_recipients.c.failure_code.in_(tuple(UNREACHABLE)),
                    broadcast_recipients.c.unreachable_done.is_(False),
                )
            )
        ).all()
    done = 0
    for bid, n in rows:
        if await mark_unreachable(deps, bid, n):
            done += 1
    return done


async def _finish_if_done(deps: Deps, bid: uuid.UUID, now: datetime) -> None:
    async with deps.sf() as s, s.begin():
        open_count = (
            await s.execute(
                select(func.count()).where(broadcast_recipients.c.broadcast_id == bid,
                                           broadcast_recipients.c.status.in_(OPEN))
            )
        ).scalar_one()
        if open_count == 0:
            await s.execute(
                update(broadcasts).where(broadcasts.c.broadcast_id == bid,
                                         broadcasts.c.state == "running")
                .values(state="finished", finished_at=now, text=None)
            )
            log.info("broadcast finished broadcast_id=%s", bid)


def token_ok(token: str | None, digests: tuple[bytes, ...]) -> bool:
    """Bearer check for the DA backend, in constant time against every accepted digest."""
    if not token or not digests:
        return False
    given = hashlib.sha256(token.encode()).digest()
    ok = False
    for d in digests:
        ok |= hmac.compare_digest(given, d)
    return ok
