"""Farmer linking (§6.6) and staff linking (§6.5), shared by both runtimes.

Every function takes the caller's database session, so the webhook worker can handle one
update in one transaction (a crash leaves nothing half done), and the RapidPro flow
routes call the same rules. Upstream calls raise ``Unavailable``; the caller decides the
answer. Nothing here calls RapidPro or OAN Messaging; Auth sends the OTP SMS itself, to
the **registered** phone, never to the shared one.

Logging: only ``contact_ref``, generated ids and fixed words. Never the chat id, Farmer
ID, phone, code or text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oan_bot_services import logs
from oan_bot_services import text_keys as tk
from oan_bot_services.crypto import masked_last_two, normalise_phone
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    contacts,
    farmer_links,
    link_failures,
    link_locks,
    link_requests,
    staff_links,
    subscriptions,
)

log = logs.get(__name__)

LOCK_AFTER = 5
LOCK_WINDOW = timedelta(hours=1)
#: Auth sets the OTP expiry (at most 30 minutes, §6.6); this is the fallback.
DEFAULT_OTP_SECONDS = 600

Out = dict[str, Any]


async def locked(s: AsyncSession, scope: str, ref: str, now: datetime) -> bool:
    until = (
        await s.execute(
            select(link_locks.c.locked_until).where(
                link_locks.c.scope == scope, link_locks.c.contact_ref == ref
            )
        )
    ).scalar_one_or_none()
    return until is not None and until > now


async def record_failure(s: AsyncSession, scope: str, ref: str, now: datetime) -> bool:
    """Records one failure; True when this one locks linking for the chat (5 in an hour)."""
    await s.execute(insert(link_failures).values(scope=scope, contact_ref=ref, at=now))
    recent = (
        await s.execute(
            select(func.count()).where(
                link_failures.c.scope == scope,
                link_failures.c.contact_ref == ref,
                link_failures.c.at > now - LOCK_WINDOW,
            )
        )
    ).scalar_one()
    if recent < LOCK_AFTER:
        return False
    until = now + LOCK_WINDOW
    stmt = pg_insert(link_locks).values(scope=scope, contact_ref=ref, locked_until=until)
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=["scope", "contact_ref"], set_={"locked_until": now + LOCK_WINDOW}
        )
    )
    await s.execute(
        delete(link_failures).where(
            link_failures.c.scope == scope, link_failures.c.contact_ref == ref
        )
    )
    log.info("linking locked scope=%s contact_ref=%s", scope, ref)
    return True


async def _clear_failures(s: AsyncSession, scope: str, ref: str) -> None:
    await s.execute(
        delete(link_failures).where(
            link_failures.c.scope == scope, link_failures.c.contact_ref == ref
        )
    )


# ------------------------------------------------------------------- farmers


async def farmer_start(
    deps: Deps,
    s: AsyncSession,
    ref: str,
    now: datetime,
    *,
    sender_id: int,
    farmer_id: str,
    shared_user_id: int,
    shared_phone: str,
    link_request_id: uuid.UUID | None = None,
) -> Out:
    """Steps 2-4: own contact, registry lookup, OTP to the registered phone.

    ``link_request_id``: the webhook worker passes one derived from the update, so a redo
    after a crash asks Auth again with the same id (Auth answers ``sent`` once)."""
    if await locked(s, "farmer", ref, now):
        return {"result": "locked", "text_key": tk.LINK_LOCKED}
    if int(shared_user_id) != int(sender_id):
        return {"result": "not_own_contact", "text_key": tk.LINK_NOT_OWN_CONTACT}
    farmer = await deps.beneficiary.farmer(farmer_id)
    # Unknown ID, inactive record and missing phone give the same answer (§6.6 step 3).
    if farmer is None or farmer.record_status != "ACTIVE" or not farmer.primary_phone:
        log.info("farmer link not found contact_ref=%s", ref)
        return {"result": "not_found", "text_key": tk.LINK_NOT_FOUND}
    registered = farmer.primary_phone
    telegram_phone = normalise_phone(str(shared_phone))
    same = telegram_phone is not None and telegram_phone == registered
    lr_id = link_request_id or uuid.uuid4()
    result, expires_in = await deps.auth.issue(lr_id, farmer.farmer_id, registered)
    if result == "rate_limited":
        return {"result": "rate_limited", "text_key": tk.LINK_RATE_LIMITED}
    stmt = pg_insert(link_requests).values(
        link_request_id=lr_id,
        contact_ref=ref,
        farmer_id=farmer.farmer_id,
        registered_phone_fp=deps.keys.fingerprint("phone", registered),
        linked_phone=(
            None if same or telegram_phone is None
            else deps.keys.seal(telegram_phone, "linked_phone")
        ),
        same_number=same,
        kebele_code=farmer.kebele_code,
        woreda_code=farmer.woreda_code,
        zone_code=farmer.zone_code,
        region_code=farmer.region_code,
        kebele_name_en=farmer.kebele_name,
        kebele_name_am=None,
        created_at=now,
        expires_at=now + timedelta(seconds=expires_in or DEFAULT_OTP_SECONDS),
    )
    await s.execute(stmt.on_conflict_do_nothing(index_elements=["link_request_id"]))
    log.info("farmer link code sent contact_ref=%s link_request_id=%s", ref, lr_id)
    return {
        "result": "code_sent",
        "link_request_id": str(lr_id),
        "text_key": tk.LINK_CODE_SENT,
        "params": {"masked_phone": masked_last_two(registered)},
    }


@dataclass(frozen=True)
class Verified:
    out: Out
    #: The link request (on success), for the RapidPro route's registry-kebele question.
    request: dict[str, Any] | None = None
    chosen_kebele: str | None = None


async def farmer_verify(
    deps: Deps,
    s: AsyncSession,
    ref: str,
    now: datetime,
    *,
    chat_id: int,
    contact_uuid: uuid.UUID | None,
    link_request_id: uuid.UUID,
    code: str,
) -> Verified:
    """Steps 5-7: verify with Auth; on success the link and the subscription (updates on).
    One link per Farmer ID and per chat: a new link replaces both."""
    if await locked(s, "farmer", ref, now):
        return Verified({"result": "locked", "text_key": tk.LINK_LOCKED})
    req = (
        await s.execute(
            select(link_requests).where(
                link_requests.c.link_request_id == link_request_id,
                link_requests.c.contact_ref == ref,
            )
        )
    ).mappings().first()
    if req is None or req["used"]:
        return Verified(await _wrong_code(s, ref, now))
    if req["expires_at"] <= now:
        return Verified({"result": "expired", "text_key": tk.LINK_CODE_EXPIRED})
    verdict = await deps.auth.verify(link_request_id, code)
    if verdict == "expired":
        return Verified({"result": "expired", "text_key": tk.LINK_CODE_EXPIRED})
    if verdict != "valid":
        return Verified(await _wrong_code(s, ref, now))
    chosen = (
        await s.execute(select(contacts).where(contacts.c.contact_ref == ref))
    ).mappings().first()
    await s.execute(
        update(link_requests).where(link_requests.c.link_request_id == link_request_id)
        .values(used=True)
    )
    for table in (farmer_links, subscriptions):
        await s.execute(
            delete(table).where(
                (table.c.farmer_id == req["farmer_id"]) | (table.c.contact_ref == ref)
            )
        )
    area = {k: req[k] for k in ("kebele_code", "woreda_code", "zone_code", "region_code")}
    await s.execute(
        insert(farmer_links).values(
            contact_ref=ref, contact_uuid=contact_uuid, farmer_id=req["farmer_id"],
            linked_phone=req["linked_phone"], registered_phone_fp=req["registered_phone_fp"],
            same_number=req["same_number"], rapidpro_synced=False, linked_at=now, **area,
        )
    )
    await s.execute(
        insert(subscriptions).values(
            contact_ref=ref, contact_uuid=contact_uuid, farmer_id=req["farmer_id"],
            language=chosen["language"] if chosen else None, updates_on=True,
            chat_id=chat_id, state="active", stopped_at=None, last_activity_at=now, **area,
        )
    )
    await _clear_failures(s, "farmer", ref)
    log.info("farmer linked contact_ref=%s same_number=%s", ref, req["same_number"])
    return Verified(
        {"result": "linked", "text_key": tk.LINK_DONE},
        request=dict(req),
        chosen_kebele=chosen["kebele_code"] if chosen else None,
    )


async def _wrong_code(s: AsyncSession, ref: str, now: datetime) -> Out:
    if await record_failure(s, "farmer", ref, now):
        return {"result": "locked", "text_key": tk.LINK_LOCKED}
    return {"result": "invalid", "text_key": tk.LINK_CODE_INVALID}


# --------------------------------------------------------------------- staff


async def staff(
    deps: Deps,
    s: AsyncSession,
    ref: str,
    now: datetime,
    *,
    code: str,
    chat_id: int,
    contact_uuid: uuid.UUID | None,
) -> Out:
    """§6.5: the code goes to R2; the DA backend stores the chat as the delivery address."""
    if await locked(s, "staff", ref, now):
        return {"result": "locked", "text_key": tk.STAFF_LOCKED}
    outcome = await deps.da.link_person(code, chat_id)
    if outcome.result != "linked" or not outcome.ref or outcome.role not in tk.ROLE_KEYS:
        if await record_failure(s, "staff", ref, now):
            return {"result": "locked", "text_key": tk.STAFF_LOCKED}
        if outcome.result == "expired":
            return {"result": "expired", "text_key": tk.STAFF_CODE_EXPIRED}
        return {"result": "invalid", "text_key": tk.STAFF_CODE_INVALID}
    stmt = pg_insert(staff_links).values(
        contact_ref=ref, contact_uuid=contact_uuid, da_ref=outcome.ref, role=outcome.role,
        group_added=False, linked_at=now,
    )
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=["contact_ref"],
            set_={"contact_uuid": contact_uuid, "da_ref": outcome.ref, "role": outcome.role,
                  "group_added": False, "linked_at": now},
        )
    )
    await _clear_failures(s, "staff", ref)
    log.info("staff linked contact_ref=%s role=%s", ref, outcome.role)
    return {
        "result": "linked",
        "role": outcome.role,
        "text_key": tk.STAFF_LINKED,
        "params": {"role_name": tk.ROLE_KEYS[outcome.role]},
    }
