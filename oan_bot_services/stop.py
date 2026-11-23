"""Stop and delete my data (§5.4). Steps, in order, each recorded in ``stop_requests`` so a
repeat (the flow again, or the worker) finishes the rest:

1. ``link``: link, subscription, queued keyed messages, link requests and lockouts, staff
   link, the contact row; broadcast copies not yet handed to OAN Messaging are dropped.
2. ``questions``: open questions closed "farmer stopped". Wave 1 has no question store,
   so the step records ``done:none``.
3. ``staff_copies``: only if RV-01 shows RapidPro can delete them; until then recorded as
   ``skipped:rv01_pending``.
4. ``media``: Garage media. Bot Services has no Garage adapter in Phase A, so it records
   ``skipped:not_configured``; deleting the RapidPro contact (step 5) removes its history.
5. ``rapidpro_contact``: the RapidPro contact, through the API. On failure the request
   stays ``pending`` and the worker retries.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import case, delete, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oan_bot_services import logs
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    broadcast_recipients,
    contacts,
    farmer_links,
    keyed_queue,
    link_failures,
    link_locks,
    link_requests,
    staff_links,
    stop_requests,
    subscriptions,
)

log = logs.get(__name__)
STEPS = ("link", "questions", "staff_copies", "media", "rapidpro_contact")


async def run_stop(deps: Deps, ref: str, contact_uuid: uuid.UUID, *, new: bool = True) -> bool:
    """Runs the steps not yet done. True when every step is done or skipped.

    ``new``: a Stop from the flow. After an earlier Stop that finished, the chat may have
    started again as a new contact, so every step runs again; a Stop still pending keeps
    the steps already done."""
    now = await deps.clock()
    async with deps.sf() as s, s.begin():
        stmt = pg_insert(stop_requests).values(
            contact_ref=ref, contact_uuid=contact_uuid, steps={}, state="pending",
            requested_at=now,
        )
        if new:
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["contact_ref"],
                    set_={
                        "steps": case(
                            (stop_requests.c.state == "done", literal({}, JSONB)),
                            else_=stop_requests.c.steps,
                        ),
                        "contact_uuid": contact_uuid,
                        "state": "pending",
                        "requested_at": now,
                        "finished_at": None,
                    },
                )
            )
        else:
            await s.execute(stmt.on_conflict_do_nothing(index_elements=["contact_ref"]))
        row = (
            await s.execute(select(stop_requests).where(stop_requests.c.contact_ref == ref))
        ).mappings().one()
    steps: dict[str, Any] = dict(row["steps"])
    target = row["contact_uuid"]
    for step in STEPS:
        if step in steps:
            continue
        outcome = await _step(deps, step, ref, target)
        if outcome is None:
            break
        steps[step] = outcome
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(stop_requests).where(stop_requests.c.contact_ref == ref).values(steps=steps)
            )
    done = all(step in steps for step in STEPS)
    if done:
        async with deps.sf() as s, s.begin():
            await s.execute(
                update(stop_requests).where(stop_requests.c.contact_ref == ref)
                .values(state="done", finished_at=await deps.clock())
            )
    log.info("stop contact_ref=%s state=%s", ref, "done" if done else "pending")
    return done


async def _step(deps: Deps, step: str, ref: str, contact_uuid: uuid.UUID) -> str | None:
    if step == "link":
        async with deps.sf() as s, s.begin():
            for table in (farmer_links, subscriptions, keyed_queue, link_requests,
                          staff_links, contacts):
                await s.execute(delete(table).where(table.c.contact_ref == ref))
            for table in (link_failures, link_locks):
                await s.execute(delete(table).where(table.c.contact_ref == ref))
            await s.execute(
                update(broadcast_recipients)
                .where(broadcast_recipients.c.contact_ref == ref,
                       broadcast_recipients.c.status == "pending")
                .values(status="failed", failure_code="farmer_stopped", chat_id=None,
                        updated_at=await deps.clock())
            )
        return "done"
    if step == "questions":
        return "done:none"
    if step == "staff_copies":
        return "skipped:rv01_pending"
    if step == "media":
        return "skipped:not_configured"
    try:
        await deps.rapidpro.delete_contact(contact_uuid)
    except Unavailable:
        log.warning("stop pending rapidpro contact_ref=%s", ref)
        return None
    return "done"


async def retry_pending(deps: Deps) -> int:
    """Worker job: finish stop requests that are still pending."""
    async with deps.sf() as s:
        rows = (
            await s.execute(
                select(stop_requests.c.contact_ref, stop_requests.c.contact_uuid)
                .where(stop_requests.c.state == "pending")
            )
        ).all()
    finished = 0
    for ref, contact_uuid in rows:
        if await run_stop(deps, ref, contact_uuid, new=False):
            finished += 1
    return finished
