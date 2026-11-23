"""Fakes for every service Bot Services calls, behind one ``httpx.MockTransport``.

Each fake checks the request against its contract file (``contract.py``) and its own
answer too, and records problems in ``Upstreams.violations``; the ``bot`` fixture fails a
test that leaves any. Test data only: phones ``+2519000001xx``, hosts ``*.example.invalid``.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
from contract import check_request, check_response

T0 = datetime(2026, 12, 1, 5, 30, tzinfo=UTC)
STUBS = "http://stubs.example.invalid"
RAPIDPRO = "http://rapidpro.example.invalid"
OAN = "http://oan-messaging.example.invalid"

#: Every value tests use that must never appear in a log line.
SENSITIVE: set[str] = set()


def sensitive(*values: object) -> None:
    SENSITIVE.update(str(v) for v in values if v not in (None, ""))


# Master Data: region ET01 > zone ET0101 > woredas ET010101, ET010102 > 2 kebeles each;
# region ET02 > zone ET0201 > woreda ET020101 > kebele ET02010101.
AREAS: dict[str, dict[str, Any]] = {}


def _area(code: str, level: str, parent: str | None) -> None:
    AREAS[code] = {
        "area_code": code,
        "level": level,
        "name_en": f"Area {code}",
        "name_am": f"[am] {code}",
        "parent_code": parent,
    }


for r, z, ws in (("ET01", "ET0101", ("ET010101", "ET010102")), ("ET02", "ET0201", ("ET020101",))):
    _area(r, "region", None)
    _area(z, "zone", r)
    for w in ws:
        _area(w, "woreda", z)
        for k in ("01", "02") if r == "ET01" else ("01",):
            _area(w + k, "kebele", w)


def area_with_parents(code: str) -> dict[str, Any]:
    out = dict(AREAS[code])
    chain = []
    parent = out["parent_code"]
    while parent:
        chain.insert(0, dict(AREAS[parent]))
        parent = AREAS[parent]["parent_code"]
    out["parents"] = chain
    return out


@dataclass
class Farmer:
    farmer_id: str
    phone: str | None
    kebele: str
    status: str = "ACTIVE"

    def json(self) -> dict[str, Any]:
        k = AREAS[self.kebele]
        w = AREAS[k["parent_code"]]
        z = AREAS[w["parent_code"]]
        phones = [{"number": self.phone, "is_primary": True}] if self.phone else []
        return {
            "farmer_id": self.farmer_id,
            "primary_phone": self.phone,
            "phones": phones,
            "region_code": z["parent_code"],
            "zone_code": z["area_code"],
            "woreda_code": w["area_code"],
            "kebele_code": self.kebele,
            "kebele_name": k["name_en"],
            "record_status": self.status,
        }


@dataclass
class Upstreams:
    violations: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    down: set[str] = field(default_factory=set)
    token: str = field(default_factory=lambda: "tok-" + secrets.token_hex(16))
    # Beneficiary
    farmers: dict[str, Farmer] = field(default_factory=dict)
    # Auth
    otps: dict[str, dict[str, Any]] = field(default_factory=dict)
    otp_code: str = "482913"
    otp_issues: dict[str, int] = field(default_factory=dict)
    # DA backend
    link_codes: dict[str, tuple[str, str] | str] = field(default_factory=dict)
    roster: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unreachable: list[tuple[str, str]] = field(default_factory=list)
    # RapidPro
    rp_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    rp_groups: dict[str, set[str]] = field(default_factory=dict)
    rp_starts: list[dict[str, Any]] = field(default_factory=list)
    rp_deleted: list[str] = field(default_factory=list)
    rp_in_flow: dict[str, str | None] = field(default_factory=dict)
    # OAN Messaging
    oan_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    oan_by_key: dict[str, str] = field(default_factory=dict)
    oan_auth: str = "oan-caller-" + secrets.token_hex(8)

    def __post_init__(self) -> None:
        sensitive(self.token, self.oan_auth, self.otp_code)

    # ------------------------------------------------------------------ routing

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host, path = url.host, url.path
        raw = request.content
        body: Any = None
        if raw:
            ctype = request.headers.get("content-type", "")
            body = parse_qs(raw.decode()) if "form" in ctype else json.loads(raw)
        query = {k: v for k, v in parse_qs(url.query.decode()).items()}
        self.calls.append((request.method, host, path))
        if host == "stubs.example.invalid":
            service, _, rest = path.lstrip("/").partition("/")
            rest = "/" + rest
            if service == "auth" and rest == "/token":
                return self._token(body)
            if service in self.down:
                return httpx.Response(503, json={"status": 503, "code": "service_unavailable"})
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                self.violations.append(f"{service}: missing or wrong token")
                return httpx.Response(401)
            name = {
                "da": "da",
                "auth": "auth",
                "beneficiary": "beneficiary",
                "master-data": "master-data",
                "marketplace": "marketplace",
            }[service]
            self.violations += check_request(name, request.method, rest, body, query)
            status, out = self._stub(service, request.method, rest, body, query)
            if out is not None or status != 204:
                self.violations += check_response(name, request.method, rest, status, out)
            return httpx.Response(status, json=out) if out is not None else httpx.Response(status)
        if host == "rapidpro.example.invalid":
            if "rapidpro" in self.down:
                return httpx.Response(503)
            return self._rapidpro(request, path, body, query)
        if host == "oan-messaging.example.invalid":
            if "oan" in self.down:
                return httpx.Response(503, json={"status": 503, "code": "service_unavailable"})
            if request.headers.get("Authorization") != f"Bearer {self.oan_auth}":
                return httpx.Response(401, json={"status": 401, "code": "unauthenticated"})
            return self._oan(request, path, body)
        self.violations.append(f"unexpected host {host}")
        return httpx.Response(404)

    def _token(self, form: dict[str, list[str]] | None) -> httpx.Response:
        if not form or form.get("grant_type") != ["client_credentials"]:
            self.violations.append("token: not client_credentials")
        return httpx.Response(
            200, json={"access_token": self.token, "expires_in": 3600, "token_type": "Bearer"}
        )

    # -------------------------------------------------------------------- stubs

    def _stub(
        self, service: str, method: str, path: str, body: Any, query: dict[str, list[str]]
    ) -> tuple[int, Any]:
        if service == "master-data":
            if path == "/areas":
                level = query["level"][0]
                parent = query.get("parent_code", [None])[0]
                items = [
                    a for a in AREAS.values() if a["level"] == level and a["parent_code"] == parent
                ]
                return 200, {"items": sorted(items, key=lambda a: a["name_en"])}
            code = path.rsplit("/", 1)[1]
            if code not in AREAS:
                return 404, None
            return 200, area_with_parents(code)
        if service == "beneficiary":
            fid = path.rsplit("/", 1)[1]
            f = self.farmers.get(fid)
            if f is None:
                return 404, {"status": 404, "code": "not_found"}
            return 200, f.json()
        if service == "auth":
            if path == "/link-otp":
                lr = body["link_request_id"]
                if lr in self.otps:
                    return 200, {"result": "sent", "expires_in": 600}
                n = self.otp_issues.get(body["farmer_id"], 0) + 1
                self.otp_issues[body["farmer_id"]] = n
                if n > 3:
                    return 200, {"result": "rate_limited"}
                self.otps[lr] = {
                    "code": self.otp_code,
                    "phone": body["phone"],
                    "used": False,
                    "farmer_id": body["farmer_id"],
                }
                return 200, {"result": "sent", "expires_in": 600}
            otp = self.otps.get(body["link_request_id"])
            if otp is None or otp["used"] or otp["code"] != body["code"]:
                return 200, {"result": "invalid"}
            if otp.get("expired"):
                return 200, {"result": "expired"}
            otp["used"] = True
            return 200, {"result": "valid"}
        if service == "da":
            if path == "/staff-links":
                entry = self.link_codes.get(body["code"])
                if entry is None:
                    return 200, {"result": "invalid"}
                if entry == "expired":
                    return 200, {"result": "expired"}
                ref, role = entry  # type: ignore[misc]
                del self.link_codes[body["code"]]
                return 200, {
                    "result": "linked",
                    "ref": ref,
                    "role": role,
                    "display_name": "Test Person",
                }
            if path == "/roster":
                return 200, {
                    "kebeles": [
                        {
                            "kebele_code": k,
                            "routing_mode": "specialism",
                            "agents": self.roster.get(k, []),
                        }
                        for k in body["kebele_codes"]
                    ]
                }
            if path.endswith("/unreachable"):
                ref = path.split("/")[2]
                self.unreachable.append((ref, body["reason"]))
                return 204, None
            return 200, {"officers": []}
        if service == "marketplace":
            if path == "/commodities":
                return 200, {
                    "items": [
                        {
                            "commodity_id": "C1",
                            "name_am": "[am] Teff",
                            "name_en": "Teff",
                            "unit": "kg",
                        }
                    ]
                }
            if path == "/markets":
                return 200, {
                    "items": [
                        {
                            "market_id": "M1",
                            "name_am": "[am] M1",
                            "name_en": "Market 1",
                            "woreda_code": "ET010101",
                        }
                    ]
                }
            return 200, {
                "items": [
                    {
                        "market_id": "M1",
                        "price_etb": 75.5,
                        "unit": "kg",
                        "price_date": "2026-12-01",
                        "source": "NMIS",
                    }
                ]
            }
        return 404, None

    # ----------------------------------------------------------------- rapidpro

    def _rapidpro(
        self, request: httpx.Request, path: str, body: Any, query: dict[str, list[str]]
    ) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("Token "):
            self.violations.append("rapidpro: no token")
        if path == "/api/v2/contacts.json":
            uuid_ = query.get("uuid", [""])[0]
            if request.method == "DELETE":
                self.rp_deleted.append(uuid_)
                return httpx.Response(204)
            if request.method == "GET":
                if uuid_ not in self.rp_in_flow:
                    return httpx.Response(200, json={"results": []})
                flow = self.rp_in_flow[uuid_]
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"uuid": uuid_, "flow": {"uuid": flow, "name": "x"} if flow else None}
                        ]
                    },
                )
            self.rp_fields.setdefault(uuid_, {}).update(body["fields"])
            return httpx.Response(200, json={"uuid": uuid_})
        if path == "/api/v2/contact_actions.json":
            for c in body["contacts"]:
                self.rp_groups.setdefault(c, set()).add(body["group"])
            return httpx.Response(204)
        if path == "/api/v2/flow_starts.json":
            self.rp_starts.append(body)
            return httpx.Response(201, json={"uuid": str(uuid.uuid4())})
        self.violations.append(f"rapidpro: unexpected {path}")
        return httpx.Response(404)

    # ---------------------------------------------------------------------- oan

    def _oan(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:
        if request.method == "POST" and path == "/api/v1/messages/telegram":
            key = request.headers.get("Idempotency-Key")
            if not key:
                return httpx.Response(422, json={"status": 422, "code": "validation_failed"})
            if set(body) != {"chat_id", "text", "purpose", "expires_at", "source_reference"}:
                self.violations.append(f"oan: unexpected fields {sorted(body)}")
            if key in self.oan_by_key:
                m = self.oan_messages[self.oan_by_key[key]]
                return httpx.Response(200, json={"id": m["id"], "status": m["status"]})
            mid = str(uuid.uuid4())
            self.oan_messages[mid] = {
                "id": mid,
                "status": "accepted",
                "failure_code": None,
                "body": body,
                "key": key,
            }
            self.oan_by_key[key] = mid
            return httpx.Response(
                202, json={"id": mid, "status": "accepted", "channel": "telegram"}
            )
        if request.method == "GET" and path.startswith("/api/v1/messages/"):
            mid = path.rsplit("/", 1)[1]
            m = self.oan_messages.get(mid)
            if m is None:
                return httpx.Response(404, json={"status": 404, "code": "not_found"})
            out = {"id": mid, "status": m["status"], "channel": "telegram"}
            if m["failure_code"]:
                out["failure_code"] = m["failure_code"]
            return httpx.Response(200, json=out)
        return httpx.Response(404)

    def settle(
        self, status: str = "sent", failure_code: str | None = None, chat_id: int | None = None
    ) -> None:
        for m in self.oan_messages.values():
            if chat_id is None or m["body"]["chat_id"] == chat_id:
                m["status"], m["failure_code"] = status, failure_code


def new_contact(telegram_id: int) -> dict[str, Any]:
    sensitive(telegram_id)
    return {"uuid": str(uuid.uuid4()), "telegram_id": telegram_id}


def rid() -> str:
    return str(uuid.uuid4())


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
