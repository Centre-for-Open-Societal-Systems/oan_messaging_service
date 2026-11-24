"""Timer and purge jobs the worker runs (one replica at a time)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, exists, select, update

from oan_bot_services import logs
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.deps import Deps
from oan_bot_services.schema import (
    broadcast_bindings,
    broadcast_recipients,
    broadcasts,
    contacts,
    conversations,
    farmer_links,
    link_failures,
    link_locks,
    link_requests,
    outbox,
    request_replays,
    seen_updates,
    staff_links,
    stop_requests,
    subscriptions,
)

log = logs.get(__name__)


async def retry_rapidpro_sync(deps: Deps) -> int:
    """Finish RapidPro updates that failed during a flow call: ``linked = yes`` on a
    linked farmer, and the ``staff`` group for a linked staff member."""
    fixed = 0
    rapidpro = deps.rapidpro
    if rapidpro is None:
        return 0
    async with deps.sf() as s:
        farmers = (
            await s.execute(
                select(farmer_links.c.contact_ref, farmer_links.c.contact_uuid)
                .where(farmer_links.c.rapidpro_synced.is_(False),
                       farmer_links.c.contact_uuid.is_not(None))
            )
        ).all()
        staff = (
            await s.execute(
                select(staff_links.c.contact_ref, staff_links.c.contact_uuid)
                .where(staff_links.c.group_added.is_(False),
                       staff_links.c.contact_uuid.is_not(None))
            )
        ).all()
    try:
        for ref, contact_uuid in farmers:
            await rapidpro.set_fields(contact_uuid, {"linked": "yes"})
            async with deps.sf() as s, s.begin():
                await s.execute(update(farmer_links).where(farmer_links.c.contact_ref == ref)
                                .values(rapidpro_synced=True))
            fixed += 1
        for ref, contact_uuid in staff:
            await rapidpro.add_to_group(contact_uuid)
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
        gone: list[str] = []
        if st.rapidpro_enabled:
            # §6.8.1 (RapidPro runtime): 12 months without activity removes the row and
            # the link. Without RapidPro there is no inactivity expiry while reachable.
            stale = now - timedelta(days=st.subscription_inactive_days)
            gone += (
                await s.execute(
                    delete(subscriptions).where(subscriptions.c.last_activity_at < stale)
                    .returning(subscriptions.c.contact_ref)
                )
            ).scalars().all()
        # Stopped or unreachable for the retention period: the row and its link go.
        expired = now - timedelta(days=st.stopped_retention_days)
        stopped = (
            await s.execute(
                delete(subscriptions).where(
                    subscriptions.c.state.in_(("stopped", "unreachable")),
                    subscriptions.c.stopped_at < expired,
                ).returning(subscriptions.c.contact_ref)
            )
        ).scalars().all()
        gone += stopped
        out["subscriptions"] = len(gone)
        if gone:
            await s.execute(delete(farmer_links).where(farmer_links.c.contact_ref.in_(gone)))
        # The chat row goes with a deleted subscription (unless the chat is staff), and a
        # chat that sent Stop without a link goes after the same period.
        no_staff = ~exists().where(staff_links.c.contact_ref == contacts.c.contact_ref)
        no_sub = ~exists().where(subscriptions.c.contact_ref == contacts.c.contact_ref)
        chats = (
            await s.execute(
                delete(contacts).where(
                    no_staff, no_sub,
                    contacts.c.contact_ref.in_(stopped) | (contacts.c.stopped_at < expired),
                ).returning(contacts.c.contact_ref)
            )
        ).scalars().all()
        out["contacts"] = len(chats)
        if chats:
            await s.execute(delete(conversations).where(conversations.c.contact_ref.in_(chats)))
        await run("seen_updates", delete(seen_updates).where(
            seen_updates.c.seen_at < now - timedelta(days=st.update_id_days)))
        await run("conversations", delete(conversations).where(conversations.c.expires_at < now))
        await run("outbox", delete(outbox).where(outbox.c.expires_at < now - timedelta(days=1)))
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
