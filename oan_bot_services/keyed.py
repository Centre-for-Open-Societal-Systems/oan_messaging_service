"""Keyed messages through RapidPro's ``notify`` flow, queued until the contact is idle
(§5.8, TG-10). Starting a flow interrupts the current one, so:

* ``/contacts/idle`` (the last node of every flow) releases the contact's queue, in order;
* the worker's one-minute timer releases queues of contacts no longer in a flow (it asks
  RapidPro, and starts ``notify`` with "skip contacts already in a flow").

Wave 1 has no producer yet (questions arrive in wave 2); ``enqueue`` checks every key
against the catalogue.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, distinct, insert, select

from oan_bot_services import logs
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.deps import Deps
from oan_bot_services.schema import keyed_queue
from oan_bot_services.text_keys import check_key

log = logs.get(__name__)


async def enqueue(
    deps: Deps,
    ref: str,
    contact_uuid: uuid.UUID,
    text_key: str,
    params: dict[str, str | int] | None = None,
    keyboard: list[str] | None = None,
) -> None:
    check_key(text_key)
    for key in keyboard or []:
        check_key(key)
    if len(keyboard or []) > 8:
        raise ValueError("at most 8 buttons")
    async with deps.sf() as s, s.begin():
        await s.execute(
            insert(keyed_queue).values(
                contact_ref=ref, contact_uuid=contact_uuid, text_key=text_key,
                params=params or {}, keyboard=keyboard or [], created_at=await deps.clock(),
            )
        )


async def release_for_contact(deps: Deps, ref: str, *, exclude_active: bool) -> int:
    """Start ``notify`` for each queued message of the contact, oldest first. Stops at the
    first RapidPro failure (the rest stay queued). Returns how many were released."""
    async with deps.sf() as s:
        rows = (
            await s.execute(
                select(keyed_queue).where(keyed_queue.c.contact_ref == ref)
                .order_by(keyed_queue.c.id)
            )
        ).mappings().all()
    released = 0
    for row in rows:
        params: dict[str, Any] = {"text_key": row["text_key"], "keyboard": row["keyboard"],
                                  **row["params"]}
        try:
            await deps.rapidpro.start_notify(
                row["contact_uuid"], params, exclude_active=exclude_active
            )
        except Unavailable:
            log.warning("keyed release deferred contact_ref=%s", ref)
            break
        async with deps.sf() as s, s.begin():
            await s.execute(delete(keyed_queue).where(keyed_queue.c.id == row["id"]))
        released += 1
    return released


async def release_idle(deps: Deps) -> int:
    """Timer job: release queues of contacts RapidPro reports as not in a flow."""
    async with deps.sf() as s:
        pending = (
            await s.execute(
                select(distinct(keyed_queue.c.contact_ref), keyed_queue.c.contact_uuid)
            )
        ).all()
    total = 0
    for ref, contact_uuid in pending:
        try:
            flow = await deps.rapidpro.current_flow(contact_uuid)
        except Unavailable:
            break
        if flow is False:  # the contact is gone: nothing can be delivered
            async with deps.sf() as s, s.begin():
                await s.execute(delete(keyed_queue).where(keyed_queue.c.contact_ref == ref))
            continue
        if flow is None:
            total += await release_for_contact(deps, ref, exclude_active=True)
    return total
