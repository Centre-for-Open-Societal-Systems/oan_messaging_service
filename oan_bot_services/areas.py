"""Master Data areas (§5.6): cached 24 hours; on failure the last copy is served and a
warning logged."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oan_bot_services import logs
from oan_bot_services.clients.http import Unavailable
from oan_bot_services.clients.services import Area
from oan_bot_services.deps import Deps
from oan_bot_services.schema import area_cache

log = logs.get(__name__)
LEVELS = ("region", "zone", "woreda", "kebele")


def _chain(raw: dict[str, Any]) -> dict[str, Any]:
    area = Area.from_json(raw)
    out: dict[str, Any] = {
        "level": area.level, "name_en": area.name_en, "name_am": area.name_am,
        area.level: area.area_code,
    }
    for p in area.parents:
        out[p.level] = p.area_code
    return out


async def area_chain(deps: Deps, code: str, now: datetime) -> dict[str, Any] | None:
    """The area's level, names and parent codes, or None for an unknown area."""
    async with deps.sf() as s:
        cached = (
            await s.execute(select(area_cache).where(area_cache.c.area_code == code))
        ).mappings().first()
    fresh = timedelta(seconds=deps.settings.area_cache_seconds)
    if cached is not None and cached["fetched_at"] > now - fresh:
        return _chain(cached["body"])
    try:
        raw = await deps.master_data.area(code)
    except Unavailable:
        if cached is None:
            raise
        log.warning("master data unavailable; serving cached area")
        return _chain(cached["body"])
    if raw is None:
        return None
    async with deps.sf() as s, s.begin():
        stmt = pg_insert(area_cache).values(area_code=code, body=raw, fetched_at=now)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["area_code"], set_={"body": raw, "fetched_at": now}
            )
        )
    return _chain(raw)


async def kebeles_under(deps: Deps, level: str, code: str) -> list[tuple[str, str]]:
    """(kebele code, woreda code) of every kebele in the area, from Master Data."""
    if level == "kebele":
        chain = await area_chain(deps, code, await deps.clock())
        if chain is None:
            return []
        return [(code, str(chain.get("woreda", code)))]
    start = LEVELS.index(level)
    parents: list[tuple[str, str | None]] = [(code, code if level == "woreda" else None)]
    for child_level in LEVELS[start + 1 :]:
        nxt: list[tuple[str, str | None]] = []
        for parent, woreda in parents:
            for child in await deps.master_data.children(child_level, parent):
                own = child.area_code if child_level == "woreda" else woreda
                nxt.append((child.area_code, own))
        parents = nxt
    return [(k, w or k) for k, w in parents]
