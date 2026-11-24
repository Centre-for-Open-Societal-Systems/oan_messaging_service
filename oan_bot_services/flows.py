"""Wave 1 flow routes (§5.8), RapidPro runtime only (``OAN_BOT_RAPIDPRO_ENABLED=true``):
location, updates, idle, stop, staff link and inbound, and farmer linking (§6.6). The
linking rules themselves are in ``linking.py``, shared with the webhook conversation.
Each function returns ``(status, body)``; the API layer stores the answer under the
request's ``request_id`` so a repeat returns it and changes nothing.

Logging: only ``contact_ref``, generated ids and fixed words. Never the chat id, Farmer
ID, phone, code or text.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oan_bot_services import broadcasts, linking, logs
from oan_bot_services import text_keys as tk
from oan_bot_services.areas import area_chain
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.clients.rapidpro import RapidPro
from oan_bot_services.deps import Deps
from oan_bot_services.errors import FlowError, invalid_choice, unavailable
from oan_bot_services.keyed import release_for_contact
from oan_bot_services.schema import contacts, farmer_links, staff_links, subscriptions
from oan_bot_services.stop import run_stop

log = logs.get(__name__)

Result = tuple[int, dict[str, Any]]


def ref_of(deps: Deps, contact: dict[str, Any]) -> str:
    return deps.keys.contact_ref(int(contact["telegram_id"]))


def rapidpro_of(deps: Deps) -> RapidPro:
    """The flow routes are mounted only when RapidPro is on."""
    if deps.rapidpro is None:
        raise unavailable()
    return deps.rapidpro


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
        if found is not None and not on:
            await broadcasts.withdraw_copies(s, ref, now)
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
    contact_uuid = uuid.UUID(str(contact["uuid"]))
    try:
        async with deps.sf() as s, s.begin():
            out = await linking.staff(deps, s, ref, now, code=body["code"],
                                      chat_id=int(contact["telegram_id"]),
                                      contact_uuid=contact_uuid)
    except Unavailable:
        raise unavailable() from None
    if out["result"] != "linked":
        return 200, out
    try:
        await rapidpro_of(deps).add_to_group(contact_uuid)
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(staff_links).where(staff_links.c.contact_ref == ref).values(group_added=True)
            )
    except Unavailable:
        log.warning("staff group add deferred contact_ref=%s", ref)  # the worker retries
    return 200, out


async def staff_inbound(deps: Deps, body: dict[str, Any]) -> Result:
    """Phase A skeleton: any staff message gets the staff menu (wave 2 adds routing)."""
    return 200, {"text_key": tk.STAFF_MENU, "params": {}, "keyboard": []}


# --------------------------------------------------------------- /farmer-link/*


async def farmer_link_start(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    now = await deps.clock()
    shared = body["shared_contact"]
    try:
        async with deps.sf() as s, s.begin():
            out = await linking.farmer_start(
                deps, s, ref, now, sender_id=int(contact["telegram_id"]),
                farmer_id=body["farmer_id"], shared_user_id=int(shared["user_id"]),
                shared_phone=str(shared["phone_number"]),
            )
    except Unavailable:
        raise unavailable() from None
    return 200, out


async def farmer_link_verify(deps: Deps, body: dict[str, Any]) -> Result:
    contact = body["contact"]
    ref = ref_of(deps, contact)
    now = await deps.clock()
    contact_uuid = uuid.UUID(str(contact["uuid"]))
    try:
        async with deps.sf() as s, s.begin():
            v = await linking.farmer_verify(
                deps, s, ref, now, chat_id=int(contact["telegram_id"]),
                contact_uuid=contact_uuid,
                link_request_id=uuid.UUID(str(body["link_request_id"])), code=body["code"],
            )
    except Unavailable:
        raise unavailable() from None
    if v.out["result"] != "linked" or v.request is None:
        return 200, v.out
    req = v.request
    try:
        await rapidpro_of(deps).set_fields(contact_uuid, {"linked": "yes"})
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(farmer_links).where(farmer_links.c.contact_ref == ref)
                .values(rapidpro_synced=True)
            )
    except Unavailable:
        log.warning("linked field deferred contact_ref=%s", ref)  # the worker retries
    ask = bool(v.chosen_kebele) and v.chosen_kebele != req["kebele_code"]
    out: dict[str, Any] = {"result": "linked", "ask_registry_kebele": ask,
                           "text_key": tk.LINK_DONE}
    if ask:
        out["params"] = {"kebele_name": req["kebele_name_en"] or req["kebele_code"]}
    return 200, out


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
        await rapidpro_of(deps).set_fields(uuid.UUID(str(contact["uuid"])), {"linked": "no"})
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
