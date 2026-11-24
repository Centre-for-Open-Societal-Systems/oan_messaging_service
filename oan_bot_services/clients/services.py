"""Clients for the services Bot Services calls in wave 1, each against its contract:

* DA backend R1 to R4: ``telegram/contracts/stubs/da-backend.openapi.yaml``
* Auth linking OTP: ``telegram/contracts/stubs/auth.openapi.yaml``
* Beneficiary API: the DA kit's ``beneficiary-api.openapi.yaml``
* Master Data: ``telegram/contracts/stubs/master-data.openapi.yaml``
* Marketplace: ``telegram/contracts/stubs/marketplace.openapi.yaml``

Bodies carry only what the contract names. The DA backend never receives a farmer's chat
id, phone or name; Auth and the beneficiary API receive the Farmer ID and phone during
linking only (§5.4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from oan_bot_services.clients.http import ServiceClient, Unavailable, json_object

Role = Literal["development_agent", "supervisor", "executive"]


@dataclass(frozen=True)
class Person:
    ref: str
    role: str
    telegram_id: int | None
    telegram_linked: bool


@dataclass(frozen=True)
class KebeleRoster:
    kebele_code: str
    routing_mode: str
    agents: list[Person] = field(default_factory=list)


@dataclass(frozen=True)
class StaffLinkResult:
    result: Literal["linked", "invalid", "expired"]
    ref: str | None = None
    role: str | None = None


def _person(raw: dict[str, Any]) -> Person:
    tid = raw.get("telegram_id")
    return Person(
        ref=str(raw["ref"]),
        role=str(raw["role"]),
        telegram_id=int(tid) if isinstance(tid, int) and not isinstance(tid, bool) else None,
        telegram_linked=bool(raw.get("telegram_linked")),
    )


class DaBackend:
    """R1 roster, R2 staff link, R3 unreachable, R4 officers (§6.4.5)."""

    R1_MAX = 200

    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def roster(self, kebele_codes: list[str]) -> list[KebeleRoster]:
        out: list[KebeleRoster] = []
        for i in range(0, len(kebele_codes), self.R1_MAX):
            chunk = kebele_codes[i : i + self.R1_MAX]
            r = await self.http.request("POST", "/roster", json={"kebele_codes": chunk})
            if r.status_code != 200:
                raise Unavailable("da", f"http_{r.status_code}")
            for k in json_object(r, "da").get("kebeles", []):
                out.append(
                    KebeleRoster(
                        kebele_code=str(k["kebele_code"]),
                        routing_mode=str(k["routing_mode"]),
                        agents=[_person(a) for a in k.get("agents", [])],
                    )
                )
        return out

    async def link_person(self, code: str, telegram_id: int) -> StaffLinkResult:
        r = await self.http.request(
            "POST", "/staff-links", json={"code": code, "telegram_id": telegram_id}
        )
        if r.status_code != 200:
            raise Unavailable("da", f"http_{r.status_code}")
        body = json_object(r, "da")
        result = body.get("result")
        if result not in ("linked", "invalid", "expired"):
            raise Unavailable("da", "malformed")
        return StaffLinkResult(result=result, ref=body.get("ref"), role=body.get("role"))

    async def report_unreachable(
        self, ref: str, reason: Literal["blocked", "chat_deleted"]
    ) -> bool:
        """R3. True when recorded; False for an unknown ref (404)."""
        r = await self.http.request(
            "POST", f"/staff-links/{quote(ref, safe='')}/unreachable",
            json={"reason": reason}, ok=(204,),
        )
        if r.status_code == 404:
            return False
        if r.status_code != 204:
            raise Unavailable("da", f"http_{r.status_code}")
        return True

    async def officers(self, level: Literal["woreda", "zone", "region"], code: str) -> list[Person]:
        r = await self.http.request("GET", "/officers", params={"level": level, "code": code})
        if r.status_code != 200:
            raise Unavailable("da", f"http_{r.status_code}")
        return [_person(p) for p in json_object(r, "da").get("officers", [])]


class AuthOtp:
    """Linking OTP (§6.6). Auth sends it to the registered phone; Bot Services never does."""

    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def issue(
        self, link_request_id: uuid.UUID, farmer_id: str, phone: str
    ) -> tuple[Literal["sent", "rate_limited"], int | None]:
        r = await self.http.request(
            "POST", "/link-otp",
            json={"link_request_id": str(link_request_id), "farmer_id": farmer_id, "phone": phone},
        )
        if r.status_code != 200:
            raise Unavailable("auth", f"http_{r.status_code}")
        body = json_object(r, "auth")
        result = body.get("result")
        if result not in ("sent", "rate_limited"):
            raise Unavailable("auth", "malformed")
        expires = body.get("expires_in")
        return result, int(expires) if isinstance(expires, int) else None

    async def verify(
        self, link_request_id: uuid.UUID, code: str
    ) -> Literal["valid", "invalid", "expired"]:
        r = await self.http.request(
            "POST", "/link-otp/verify", json={"link_request_id": str(link_request_id), "code": code}
        )
        if r.status_code != 200:
            raise Unavailable("auth", f"http_{r.status_code}")
        result = json_object(r, "auth").get("result")
        if result not in ("valid", "invalid", "expired"):
            raise Unavailable("auth", "malformed")
        return result  # type: ignore[no-any-return]


@dataclass(frozen=True)
class Farmer:
    farmer_id: str
    primary_phone: str | None
    region_code: str
    zone_code: str
    woreda_code: str
    kebele_code: str
    kebele_name: str | None
    record_status: str


class Beneficiary:
    """``GET /api/v1/farmers/{farmer_id}`` (ID-01). The name is not read or stored here."""

    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def farmer(self, farmer_id: str) -> Farmer | None:
        r = await self.http.request("GET", f"/api/v1/farmers/{quote(farmer_id, safe='')}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise Unavailable("beneficiary", f"http_{r.status_code}")
        b = json_object(r, "beneficiary")
        primary = b.get("primary_phone")
        if not primary:
            for p in b.get("phones") or []:
                if isinstance(p, dict) and p.get("is_primary"):
                    primary = p.get("number")
        return Farmer(
            farmer_id=str(b["farmer_id"]),
            primary_phone=str(primary) if primary else None,
            region_code=str(b["region_code"]),
            zone_code=str(b["zone_code"]),
            woreda_code=str(b["woreda_code"]),
            kebele_code=str(b["kebele_code"]),
            kebele_name=b.get("kebele_name"),
            record_status=str(b.get("record_status", "")),
        )


@dataclass(frozen=True)
class Area:
    area_code: str
    level: str
    name_en: str
    name_am: str
    parent_code: str | None
    parents: list[Area] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Area:
        return cls(
            area_code=str(raw["area_code"]),
            level=str(raw["level"]),
            name_en=str(raw.get("name_en", "")),
            name_am=str(raw.get("name_am", "")),
            parent_code=raw.get("parent_code"),
            parents=[cls.from_json(p) for p in raw.get("parents", [])],
        )

    def parent(self, level: str) -> str | None:
        for p in self.parents:
            if p.level == level:
                return p.area_code
        return None


class MasterData:
    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def area(self, code: str) -> dict[str, Any] | None:
        r = await self.http.request("GET", f"/areas/{quote(code, safe='')}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise Unavailable("master_data", f"http_{r.status_code}")
        return json_object(r, "master_data")

    async def children(self, level: str, parent_code: str | None) -> list[Area]:
        params: dict[str, str] = {"level": level}
        if parent_code is not None:
            params["parent_code"] = parent_code
        r = await self.http.request("GET", "/areas", params=params)
        if r.status_code != 200:
            raise Unavailable("master_data", f"http_{r.status_code}")
        return [Area.from_json(a) for a in json_object(r, "master_data").get("items", [])]


class Marketplace:
    """Prices (§6.2). No personal data is sent. Wave 1 only builds the client."""

    def __init__(self, http: ServiceClient) -> None:
        self.http = http

    async def _items(self, path: str, params: Any = None) -> list[dict[str, Any]]:
        r = await self.http.request("GET", path, params=params)
        if r.status_code != 200:
            raise Unavailable("marketplace", f"http_{r.status_code}")
        return list(json_object(r, "marketplace").get("items", []))

    async def commodities(self) -> list[dict[str, Any]]:
        return await self._items("/commodities")

    async def markets(self, woreda_code: str | None = None) -> list[dict[str, Any]]:
        return await self._items("/markets", {"woreda_code": woreda_code} if woreda_code else None)

    async def latest_prices(self, commodity_id: str, market_ids: list[str]) -> list[dict[str, Any]]:
        params = [("commodity_id", commodity_id)] + [("market_id", m) for m in market_ids[:5]]
        return await self._items("/prices/latest", params)
