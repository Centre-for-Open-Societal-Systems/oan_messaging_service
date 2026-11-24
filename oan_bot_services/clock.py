"""The one clock Bot Services reads (contracts/stubs/clock.md).

With ``OAN_BOT_CLOCK_URL`` set (local stack only; ``BotSettings`` refuses it elsewhere) the
time comes from the fake clock, so a test can move it forward. Otherwise the system clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

Clock = Callable[[], Awaitable[datetime]]


async def system_now() -> datetime:
    return datetime.now(UTC)


def make_clock(clock_url: str | None, client: httpx.AsyncClient | None = None) -> Clock:
    if not clock_url:
        return system_now
    http = client or httpx.AsyncClient(timeout=5.0, trust_env=False)

    async def remote_now() -> datetime:
        r = await http.get(clock_url)
        r.raise_for_status()
        now = datetime.fromisoformat(str(r.json()["now"]))
        if now.tzinfo is None:
            raise ValueError("fake clock returned a time without an offset")
        return now.astimezone(UTC)

    return remote_now


def fixed_clock(now: datetime) -> Clock:
    """For tests: a settable clock (``clock.now = ...``)."""

    class _Fixed:
        def __init__(self) -> None:
            self.now = now

        async def __call__(self) -> datetime:
            return self.now

    return _Fixed()
