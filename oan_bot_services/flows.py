"""Wave 1 flow routes (§5.8): location, updates, idle, stop, staff link and inbound, and
farmer linking (§6.6). Each function returns ``(status, body)``; the API layer stores the
answer under the request's ``request_id`` so a repeat returns it and changes nothing.

Logging: only ``contact_ref``, generated ids and fixed words. Never the chat id, Farmer
ID, phone, code or text.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oan_bot_services import logs
from oan_bot_services import text_keys as tk
from oan_bot_services.areas import area_chain
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.crypto import masked_last_two, normalise_phone
from oan_bot_services.deps import Deps
from oan_bot_services.errors import FlowError, invalid_choice, unavailable
from oan_bot_services.keyed import release_for_contact
from oan_bot_services.schema import (
    contacts,
    farmer_links,
    link_failures,
    link_locks,
    link_requests,
    staff_links,
    subscriptions,
)
from oan_bot_services.stop import run_stop

log = logs.get(__name__)

Result = tuple[int, dict[str, Any]]
LOCK_AFTER = 5
LOCK_WINDOW = timedelta(hours=1)
#: Auth sets the OTP expiry (at most 30 minutes, §6.6); this is the fallback.
DEFAULT_OTP_SECONDS = 600


def ref_of(deps: Deps, contact: dict[str, Any]) -> str:
    return deps.keys.contact_ref(int(contact["telegram_id"]))


# ------------------------------------------------------------------------ lockout


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


# ------------------------------------------------------------------- /contacts/*


async def set_location(deps: Deps, body: dict[str, Any]) -> Result:
    ref = ref_of(deps, body["contact"])
    now = await deps.clock()
    try:
        chain = await area_chain(deps, body["kebele_code"], now)
    except Unavailable:
        raise unavailable() from None
    if chain is None or chain.get("level") != "kebele":
        raise invalid_choice()
    codes = {
        "kebele_code": body["kebele_code"],
        "woreda_code": chain["woreda"],
        "zone_code": chain["zone"],
        "region_code": chain["region"],
    }
    async with deps.sf() as s, s.begin():
        await _upsert_contact(s, ref, body["contact"]["uuid"], now,
                              language=body["language"], **codes)
    log.info("location saved contact_ref=%s", ref)
    return 200, codes


async def _upsert_contact(
    s: AsyncSession, ref: str, contact_uuid: str, now: datetime, **values: Any
) -> None:
    stmt = pg_insert(contacts).values(
        contact_ref=ref, contact_uuid=uuid.UUID(str(contact_uuid)), updated_at=now, **values
    )
    await s.execute(
        stmt.on_conflict_do_update(
            index_elements=["contact_ref"],
            set_={"contact_uuid": stmt.excluded.contact_uuid, "updated_at": now, **values},
        )
    )


async def set_updates(deps: Deps, body: dict[str, Any]) -> Result:
    ref = ref_of(deps, body["contact"])
    now = await deps.clock()
    on = body["updates"] == "on"
    async with deps.sf() as s, s.begin():
        # Turning updates on is the farmer talking to the bot: the chat works again.
        values: dict[str, Any] = {"updates_on": on, "last_activity_at": now}
        if on:
            values["state"] = "active"
        found = (
            await s.execute(
                update(subscriptions).where(subscriptions.c.contact_ref == ref)
                .values(**values).returning(subscriptions.c.contact_ref)
            )
        ).first()
    if found is None:
        return 200, {"result": "not_linked", "text_key": tk.LINK_INVITE}
    log.info("updates set contact_ref=%s on=%s", ref, on)
    return 200, {"result": "ok", "text_key": tk.UPDATES_ON if on else tk.UPDATES_OFF}


async def contact_idle(deps: Deps, body: dict[str, Any]) -> Result:
    ref = ref_of(deps, body["contact"])
    now = await deps.clock()
    async with deps.sf() as s, s.begin():
        await s.execute(
            update(subscriptions).where(subscriptions.c.contact_ref == ref)
            .values(last_activity_at=now)
        )
    released = await release_for_contact(deps, ref, exclude_active=False)
    return 200, {"released": released}


async def contact_stop(deps: Deps, body: dict[str, Any]) -> Result:
    ref = ref_of(deps, body["contact"])
    done = await run_stop(deps, ref, uuid.UUID(str(body["contact"]["uuid"])))
    return 200, {"result": "done" if done else "pending", "text_key": tk.STOP_DONE}


# ---------------------------------------------------------------------- /staff/*


async def staff_link(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    now = await deps.clock()
    async with deps.sf() as s:
        if await locked(s, "staff", ref, now):
            return 200, {"result": "locked", "text_key": tk.STAFF_LOCKED}
    try:
        outcome = await deps.da.link_person(body["code"], int(contact["telegram_id"]))
    except Unavailable:
        raise unavailable() from None
    if outcome.result != "linked" or not outcome.ref or outcome.role not in tk.ROLE_KEYS:
        async with deps.sf() as s, s.begin():
            now_locked = await record_failure(s, "staff", ref, now)
        if now_locked:
            return 200, {"result": "locked", "text_key": tk.STAFF_LOCKED}
        if outcome.result == "expired":
            return 200, {"result": "expired", "text_key": tk.STAFF_CODE_EXPIRED}
        return 200, {"result": "invalid", "text_key": tk.STAFF_CODE_INVALID}
    contact_uuid = uuid.UUID(str(contact["uuid"]))
    async with deps.sf() as s, s.begin():
        stmt = pg_insert(staff_links).values(
            contact_ref=ref, contact_uuid=contact_uuid, da_ref=outcome.ref, role=outcome.role,
            group_added=False, linked_at=now,
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["contact_ref"],
                set_={"contact_uuid": contact_uuid, "da_ref": outcome.ref,
                      "role": outcome.role, "group_added": False, "linked_at": now},
            )
        )
        await s.execute(
            delete(link_failures).where(
                link_failures.c.scope == "staff", link_failures.c.contact_ref == ref
            )
        )
    try:
        await deps.rapidpro.add_to_group(contact_uuid)
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(staff_links).where(staff_links.c.contact_ref == ref).values(group_added=True)
            )
    except Unavailable:
        log.warning("staff group add deferred contact_ref=%s", ref)  # the worker retries
    log.info("staff linked contact_ref=%s role=%s", ref, outcome.role)
    return 200, {
        "result": "linked",
        "role": outcome.role,
        "text_key": tk.STAFF_LINKED,
        "params": {"role_name": tk.ROLE_KEYS[outcome.role]},
    }


async def staff_inbound(deps: Deps, body: dict[str, Any]) -> Result:
    """Phase A skeleton: any staff message gets the staff menu (wave 2 adds routing)."""
    return 200, {"text_key": tk.STAFF_MENU, "params": {}, "keyboard": []}


# --------------------------------------------------------------- /farmer-link/*


async def farmer_link_start(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    now = await deps.clock()
    async with deps.sf() as s:
        if await locked(s, "farmer", ref, now):
            return 200, {"result": "locked", "text_key": tk.LINK_LOCKED}
    shared = body["shared_contact"]
    if int(shared["user_id"]) != int(contact["telegram_id"]):
        return 200, {"result": "not_own_contact", "text_key": tk.LINK_NOT_OWN_CONTACT}
    try:
        farmer = await deps.beneficiary.farmer(body["farmer_id"])
    except Unavailable:
        raise unavailable() from None
    # Unknown ID, inactive record and missing phone give the same answer (§6.6 step 3).
    if farmer is None or farmer.record_status != "ACTIVE" or not farmer.primary_phone:
        log.info("farmer link not found contact_ref=%s", ref)
        return 200, {"result": "not_found", "text_key": tk.LINK_NOT_FOUND}
    registered = farmer.primary_phone
    telegram_phone = normalise_phone(str(shared["phone_number"]))
    same = telegram_phone is not None and telegram_phone == registered
    link_request_id = uuid.uuid4()
    try:
        result, expires_in = await deps.auth.issue(link_request_id, farmer.farmer_id, registered)
    except Unavailable:
        raise unavailable() from None
    if result == "rate_limited":
        return 200, {"result": "rate_limited", "text_key": tk.LINK_RATE_LIMITED}
    async with deps.sf() as s, s.begin():
        await s.execute(
            insert(link_requests).values(
                link_request_id=link_request_id,
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
        )
    log.info("farmer link code sent contact_ref=%s link_request_id=%s", ref, link_request_id)
    return 200, {
        "result": "code_sent",
        "link_request_id": str(link_request_id),
        "text_key": tk.LINK_CODE_SENT,
        "params": {"masked_phone": masked_last_two(registered)},
    }


async def farmer_link_verify(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    now = await deps.clock()
    link_request_id = uuid.UUID(str(body["link_request_id"]))
    async with deps.sf() as s:
        if await locked(s, "farmer", ref, now):
            return 200, {"result": "locked", "text_key": tk.LINK_LOCKED}
        req = (
            await s.execute(
                select(link_requests).where(
                    link_requests.c.link_request_id == link_request_id,
                    link_requests.c.contact_ref == ref,
                )
            )
        ).mappings().first()
    if req is None or req["used"]:
        return await _wrong_code(deps, ref, now)
    if req["expires_at"] <= now:
        return 200, {"result": "expired", "text_key": tk.LINK_CODE_EXPIRED}
    try:
        verdict = await deps.auth.verify(link_request_id, body["code"])
    except Unavailable:
        raise unavailable() from None
    if verdict == "expired":
        return 200, {"result": "expired", "text_key": tk.LINK_CODE_EXPIRED}
    if verdict != "valid":
        return await _wrong_code(deps, ref, now)
    contact_uuid = uuid.UUID(str(contact["uuid"]))
    chat_id = int(contact["telegram_id"])
    async with deps.sf() as s, s.begin():
        chosen = (
            await s.execute(select(contacts).where(contacts.c.contact_ref == ref))
        ).mappings().first()
        await s.execute(
            update(link_requests).where(link_requests.c.link_request_id == link_request_id)
            .values(used=True)
        )
        # One link per Farmer ID and per chat: a new link replaces both (§6.6 step 7).
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
                chat_id=chat_id, state="active", last_activity_at=now, **area,
            )
        )
        await s.execute(
            delete(link_failures).where(
                link_failures.c.scope == "farmer", link_failures.c.contact_ref == ref
            )
        )
    try:
        await deps.rapidpro.set_fields(contact_uuid, {"linked": "yes"})
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(farmer_links).where(farmer_links.c.contact_ref == ref)
                .values(rapidpro_synced=True)
            )
    except Unavailable:
        log.warning("linked field deferred contact_ref=%s", ref)  # the worker retries
    log.info("farmer linked contact_ref=%s same_number=%s", ref, req["same_number"])
    chosen_kebele = chosen["kebele_code"] if chosen else None
    ask = bool(chosen_kebele) and chosen_kebele != req["kebele_code"]
    out: dict[str, Any] = {"result": "linked", "ask_registry_kebele": ask,
                           "text_key": tk.LINK_DONE}
    if ask:
        name = req["kebele_name_en"] or req["kebele_code"]
        out["params"] = {"kebele_name": name}
    return 200, out


async def _wrong_code(deps: Deps, ref: str, now: datetime) -> Result:
    async with deps.sf() as s, s.begin():
        now_locked = await record_failure(s, "farmer", ref, now)
    if now_locked:
        return 200, {"result": "locked", "text_key": tk.LINK_LOCKED}
    return 200, {"result": "invalid", "text_key": tk.LINK_CODE_INVALID}


async def farmer_unlink(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    async with deps.sf() as s, s.begin():
        gone = (
            await s.execute(
                delete(farmer_links).where(farmer_links.c.contact_ref == ref)
                .returning(farmer_links.c.contact_ref)
            )
        ).first()
        await s.execute(delete(subscriptions).where(subscriptions.c.contact_ref == ref))
    if gone is None:
        return 200, {"result": "not_linked", "text_key": tk.LINK_NONE}
    try:
        await deps.rapidpro.set_fields(uuid.UUID(str(contact["uuid"])), {"linked": "no"})
    except Unavailable:
        log.warning("linked field not cleared contact_ref=%s", ref)
    log.info("farmer unlinked contact_ref=%s", ref)
    return 200, {"result": "unlinked", "text_key": tk.LINK_REMOVED}


__all__ = [
    "FlowError",
    "contact_idle",
    "contact_stop",
    "farmer_link_start",
    "farmer_link_verify",
    "farmer_unlink",
    "set_location",
    "set_updates",
    "staff_inbound",
    "staff_link",
]
