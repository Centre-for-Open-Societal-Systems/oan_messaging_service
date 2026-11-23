"""Timer and purge jobs the worker runs (one replica at a time)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select, update

from oan_bot_services import logs
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    broadcast_bindings,
    broadcast_recipients,
    broadcasts,
    farmer_links,
    link_failures,
    link_locks,
    link_requests,
    request_replays,
    staff_links,
    stop_requests,
    subscriptions,
)

log = logs.get(__name__)


async def retry_rapidpro_sync(deps: Deps) -> int:
    """Finish RapidPro updates that failed during a flow call: ``linked = yes`` on a
    linked farmer, and the ``staff`` group for a linked staff member."""
    fixed = 0
    async with deps.sf() as s:
        farmers = (
            await s.execute(
                select(farmer_links.c.contact_ref, farmer_links.c.contact_uuid)
                .where(farmer_links.c.rapidpro_synced.is_(False))
            )
        ).all()
        staff = (
            await s.execute(
                select(staff_links.c.contact_ref, staff_links.c.contact_uuid)
                .where(staff_links.c.group_added.is_(False))
            )
        ).all()
    try:
        for ref, contact_uuid in farmers:
            await deps.rapidpro.set_fields(contact_uuid, {"linked": "yes"})
            async with deps.sf() as s, s.begin():
                await s.execute(update(farmer_links).where(farmer_links.c.contact_ref == ref)
                                .values(rapidpro_synced=True))
            fixed += 1
        for ref, contact_uuid in staff:
            await deps.rapidpro.add_to_group(contact_uuid)
            async with deps.sf() as s, s.begin():
                await s.execute(update(staff_links).where(staff_links.c.contact_ref == ref)
                                .values(group_added=True))
            fixed += 1
    except Unavailable:
        log.warning("rapidpro sync deferred")
    return fixed


async def purge(deps: Deps) -> dict[str, int]:
    """Delete what is past its keep time. Returns counts per table (no identifiers)."""
    now = await deps.clock()
    st = deps.settings
    out: dict[str, int] = {}
    async with deps.sf() as s, s.begin():
        async def run(name: str, stmt: object) -> None:
            result = await s.execute(stmt)  # type: ignore[call-overload]
            out[name] = int(getattr(result, "rowcount", 0) or 0)

        await run("request_replays", delete(request_replays).where(
            request_replays.c.created_at < now - timedelta(days=st.replay_days)))
        await run("link_requests", delete(link_requests).where(
            link_requests.c.expires_at < now - timedelta(days=1)))
        await run("link_failures", delete(link_failures).where(
            link_failures.c.at < now - timedelta(hours=1)))
        await run("link_locks", delete(link_locks).where(link_locks.c.locked_until < now))
        await run("stop_requests", delete(stop_requests).where(
            stop_requests.c.state == "done",
            stop_requests.c.finished_at < now - timedelta(days=st.replay_days)))
        # §6.8.1: 12 months (configurable) without activity removes the row and the link.
        stale = now - timedelta(days=st.subscription_inactive_days)
        gone = (
            await s.execute(
                delete(subscriptions).where(subscriptions.c.last_activity_at < stale)
                .returning(subscriptions.c.contact_ref)
            )
        ).scalars().all()
        out["subscriptions"] = len(gone)
        if gone:
            await s.execute(delete(farmer_links).where(farmer_links.c.contact_ref.in_(gone)))
        keep = now - timedelta(days=st.broadcast_binding_days)
        await run("broadcast_bindings", delete(broadcast_bindings).where(
            broadcast_bindings.c.created_at < keep))
        old = (
            await s.execute(
                select(broadcasts.c.broadcast_id).where(
                    broadcasts.c.state == "finished", broadcasts.c.finished_at < keep
                )
            )
        ).scalars().all()
        if old:
            await s.execute(delete(broadcast_recipients).where(
                broadcast_recipients.c.broadcast_id.in_(old)))
            await s.execute(delete(broadcasts).where(broadcasts.c.broadcast_id.in_(old)))
        out["broadcasts"] = len(old)
    log.info("purge %s", " ".join(f"{k}={v}" for k, v in sorted(out.items())))
    return out
