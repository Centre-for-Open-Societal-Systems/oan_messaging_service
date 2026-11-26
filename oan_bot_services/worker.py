"""The worker: one replica at a time, holding a PostgreSQL advisory lock.

A second copy waits for the lock and runs nothing until the first stops. While it holds
the lock, the worker:

* every few seconds: (without RapidPro) handles stored webhook updates and sends queued
  replies; cancels broadcast copies marked by Updates off or Stop; submits pending
  broadcast recipients and polls results;
* every minute (the timer job): retries TG-09 marking; with RapidPro also releases keyed
  messages of idle contacts, finishes pending Stop requests and retries RapidPro updates
  (none of these run when ``OAN_BOT_RAPIDPRO_ENABLED`` is false);
* every 15 minutes: purges.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from oan_bot_services import broadcasts, conversation, jobs, keyed, logs, stop
from oan_bot_services.deps import Deps

log = logs.get(__name__)

LOCK_KEY = 0x4F414E42  # "OANB"
TICK_SECONDS = 2.0
TIMER_SECONDS = 60.0
PURGE_SECONDS = 900.0
WAIT_SECONDS = 5.0


async def _guard(name: str, job: Callable[[], Awaitable[object]]) -> None:
    try:
        await job()
    except Exception as exc:  # one failing job never stops the others
        log.error("job failed job=%s error_type=%s", name, type(exc).__name__)


async def timer_job(deps: Deps) -> None:
    if deps.settings.rapidpro_enabled:
        await _guard("keyed_release", lambda: keyed.release_idle(deps))
        await _guard("stop_retry", lambda: stop.retry_pending(deps))
    await _guard("unreachable_retry", lambda: broadcasts.retry_unreachable(deps))
    if deps.settings.rapidpro_enabled:
        await _guard("rapidpro_sync", lambda: jobs.retry_rapidpro_sync(deps))


async def conversation_job(deps: Deps) -> None:
    """Reduced runtime: stored webhook updates, then the replies they queued."""
    if deps.settings.rapidpro_enabled:
        return
    await _guard("updates", lambda: conversation.process_updates(deps))
    await _guard("replies", lambda: conversation.send_outbox(deps))


async def broadcast_job(deps: Deps) -> None:
    await _guard("broadcast_cancel", lambda: broadcasts.cancel_pending(deps))
    await _guard("broadcast_submit", lambda: broadcasts.submit_pending(deps))
    await _guard("broadcast_poll", lambda: broadcasts.poll_results(deps))


async def run(
    deps: Deps,
    stop_event: asyncio.Event,
    *,
    tick: float = TICK_SECONDS,
    timer: float = TIMER_SECONDS,
    purge_every: float = PURGE_SECONDS,
    wait: float = WAIT_SECONDS,
    on_locked: Callable[[], None] | None = None,
) -> None:
    async with deps.engine.connect() as conn:
        while not stop_event.is_set():
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}))
            await conn.commit()
            if got.scalar_one():
                break
            log.info("worker waiting for the lock")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except TimeoutError:
                pass
        else:
            return
        log.info("worker holds the lock")
        if on_locked is not None:
            on_locked()
        last_timer = last_purge = 0.0
        try:
            while not stop_event.is_set():
                await conversation_job(deps)
                await broadcast_job(deps)
                now = time.monotonic()
                if now - last_timer >= timer:
                    await timer_job(deps)
                    last_timer = now
                if now - last_purge >= purge_every:
                    await _guard("purge", lambda: jobs.purge(deps))
                    last_purge = now
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=tick)
                except TimeoutError:
                    pass
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            await conn.commit()
            log.info("worker released the lock")
