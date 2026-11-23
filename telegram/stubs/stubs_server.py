"""One stub server for Telegram Phase A: DA backend, Auth, beneficiary API, Master Data,
Marketplace and the fake clock.

    OAN_STUBS_CLIENT_ID=... OAN_STUBS_CLIENT_SECRET=... python3 stubs_server.py --port 8200

Contracts (frozen, in ``telegram/contracts/``):

* ``/da``           stubs/da-backend.openapi.yaml   (R1 to R4)
* ``/auth``         stubs/auth.openapi.yaml         (token, linking OTP)
* ``/beneficiary``  integrations/kits/oan_da_app_services/broadcast-channels/docs/
                    beneficiary-api.openapi.yaml    (paths kept as ``/api/v1/farmers...``)
* ``/master-data``  stubs/master-data.openapi.yaml
* ``/marketplace``  stubs/marketplace.openapi.yaml
* ``/clock``        stubs/clock.md

Test-only routes (no token): ``POST /<service>/_test/down``, ``POST /<service>/_test/slow``,
``POST /_test/reset``, ``GET /auth/_test/codes``, ``POST /da/_test/link-codes``.
See README.md. Standard library only. Test data only.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
EAT = timezone(timedelta(hours=3))
RESET_TIME = datetime(2026, 12, 1, 8, 30, 0, tzinfo=EAT)

SERVICES = ("da", "auth", "beneficiary", "master-data", "marketplace")
LEVELS = ("region", "zone", "woreda", "kebele")
SPECIALISATIONS = (
    "CROP_PRODUCTION",
    "LIVESTOCK",
    "NATURAL_RESOURCES",
    "AGRONOMY",
    "HORTICULTURE",
    "OTHER",
)
AREA_CODE = re.compile(r"^[A-Z0-9]{2,16}$")
LINK_CODE = re.compile(r"^[A-Za-z0-9]{8}$")
OTP_CODE = re.compile(r"^[0-9]{4,8}$")
ET_PHONE = re.compile(r"^\+251[79][0-9]{8}$")
BENEFICIARY_CODE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")

TOKEN_TTL = 3600  # seconds of real time
OTP_TTL = 600  # seconds of fake time; Auth contract allows at most 1800
OTP_LIMIT = 3  # issues per Farmer ID ...
OTP_WINDOW = timedelta(minutes=15)  # ... in this window
LINK_CODE_TTL = timedelta(minutes=15)
DEFAULT_SLOW_SECONDS = 6.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, errors: list | None = None):
        super().__init__(code)
        self.status, self.code, self.errors = status, code, errors


def _err(field: str, code: str) -> dict:
    return {"field": field, "code": code}


# ---------------------------------------------------------------------------
# Fake clock


class Clock:
    """Fake time. ``running`` (default): moves on with real time from the last reset, set or
    advance. ``frozen``: stands still until a test moves it. Never goes backwards."""

    def __init__(self, mode: str = "running"):
        if mode not in ("running", "frozen"):
            raise ValueError("clock mode must be running or frozen")
        self.mode = mode
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self._base, self._mono = RESET_TIME, time.monotonic()

    def now(self) -> datetime:
        with self.lock:
            return self._now()

    def _now(self) -> datetime:
        if self.mode == "frozen":
            return self._base
        return self._base + timedelta(seconds=time.monotonic() - self._mono)

    def advance(self, seconds: float) -> datetime:
        with self.lock:
            self._base, self._mono = self._now() + timedelta(seconds=seconds), time.monotonic()
            return self._base

    def set(self, when: datetime) -> datetime:
        with self.lock:
            if when < self._now():
                raise ApiError(422, "clock_backwards", [_err("now", "earlier_than_current")])
            self._base, self._mono = when, time.monotonic()
            return self._base


def fmt_time(t: datetime) -> str:
    return t.astimezone(EAT).isoformat()


# ---------------------------------------------------------------------------
# State


class State:
    def __init__(
        self,
        seed: dict,
        client_id: str,
        client_secret: str,
        clock: Clock,
        slow_seconds: float = DEFAULT_SLOW_SECONDS,
    ):
        self.seed = seed
        self.client_id, self.client_secret = client_id, client_secret
        self.clock = clock
        self.slow_seconds = slow_seconds
        self.lock = threading.RLock()
        self.tokens: dict[str, float] = {}  # token -> monotonic expiry; kept across resets
        self.areas = {a["area_code"]: a for a in seed["areas"]}
        self.farmers = seed["farmers"]
        self.commodities = seed["commodities"]
        self.markets = seed["markets"]
        self.missing_prices = {(m["commodity_id"], m["market_id"]) for m in seed["missing_prices"]}
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.people = {p["ref"]: dict(p) for p in self.seed["people"]}
            self.link_codes = {
                c["code"]: {"ref": c["ref"], "issued_at": RESET_TIME, "used": False}
                for c in self.seed["link_codes"]
            }
            self.otps: dict[str, dict] = {}  # link_request_id -> record
            self.otp_issues: dict[str, list[datetime]] = {}  # farmer_id -> issue times
            self.down = {s: False for s in SERVICES}
            self.slow = {s: False for s in SERVICES}
            self.clock.reset()


# ---------------------------------------------------------------------------
# Small validation helpers


def check_object(body, required: tuple, allowed: tuple) -> list:
    if not isinstance(body, dict):
        return [_err("body", "must_be_object")]
    errors = [_err(k, "required") for k in required if k not in body]
    errors += [_err(k, "unknown_field") for k in body if k not in allowed]
    return errors


def check_query(q: dict, allowed: tuple) -> list:
    errors = [_err(k, "unknown_field") for k in q if k not in allowed]
    errors += [_err(k, "repeated") for k, v in q.items() if k in allowed and len(v) > 1]
    return errors


def is_uuid(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        uuid.UUID(v)
    except ValueError:
        return False
    return True


def parse_time(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    state: State
    server_version = "oan-telegram-stubs"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: paths can carry Farmer IDs
        pass

    # -- plumbing ---------------------------------------------------------------

    def _send(
        self,
        status: int,
        body: dict | None,
        content_type: str = "application/json",
        headers: dict | None = None,
    ) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _problem(self, e: ApiError) -> None:
        body = {
            "type": "about:blank",
            "title": e.code.replace("_", " "),
            "status": e.status,
            "code": e.code,
        }
        if e.errors:
            body["errors"] = e.errors
        headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
        self._send(e.status, body, "application/problem+json", headers)

    def _body_raw(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self, raw: bytes, optional: bool = False):
        if not raw.strip():
            if optional:
                return {}
            raise ApiError(422, "validation_failed", [_err("body", "required")])
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ApiError(422, "invalid_json") from exc

    def _authorise(self) -> None:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            raise ApiError(401, "unauthenticated")
        token = h[len("Bearer ") :].strip()
        with self.state.lock:
            exp = self.state.tokens.get(token)
        if exp is None or exp < time.monotonic():
            raise ApiError(401, "unauthenticated")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        raw = self._body_raw()  # always drain, so keep-alive stays in step
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        q = parse_qs(url.query, keep_blank_values=True)
        try:
            self._route(method, parts, q, raw)
        except ApiError as e:
            self._problem(e)
        except Exception:  # noqa: BLE001 - never leak details
            self._problem(ApiError(500, "internal_error"))

    def _route(self, method: str, parts: list, q: dict, raw: bytes) -> None:
        st = self.state
        if not parts:
            raise ApiError(404, "not_found")
        # global test routes and the clock: no token, no fault switches
        if parts == ["_test", "reset"] and method == "POST":
            st.reset()
            return self._send(200, {"ok": True, "now": fmt_time(st.clock.now())})
        if parts[0] == "clock":
            return self._clock(method, parts[1:], q, raw)
        if parts == ["healthz"]:
            return self._send(200, {"ok": True})

        svc, rest = parts[0], parts[1:]
        if svc not in SERVICES:
            raise ApiError(404, "not_found")

        if rest[:1] == ["_test"]:
            return self._test_route(svc, method, rest[1:], q, raw)

        # Auth's token route stands in for Keycloak; Auth down does not stop it.
        if svc == "auth" and rest == ["token"]:
            if method != "POST":
                raise ApiError(405, "method_not_allowed")
            return self._token(raw)

        if st.down[svc]:
            raise ApiError(503, "service_unavailable")
        if st.slow[svc]:
            time.sleep(st.slow_seconds)
        self._authorise()

        handler = getattr(self, "_svc_" + svc.replace("-", "_"))
        handler(method, rest, q, raw)

    # -- test routes ------------------------------------------------------------

    def _test_route(self, svc: str, method: str, rest: list, q: dict, raw: bytes) -> None:
        st = self.state
        if method == "POST" and rest in (["down"], ["slow"]):
            body = self._json(raw, optional=True)
            errs = check_object(body, (), ("on",))
            if not errs and not isinstance(body.get("on", True), bool):
                errs = [_err("on", "must_be_boolean")]
            if errs:
                raise ApiError(422, "validation_failed", errs)
            with st.lock:
                getattr(st, rest[0])[svc] = body.get("on", True)
            return self._send(200, {"service": svc, "down": st.down[svc], "slow": st.slow[svc]})
        if svc == "auth" and rest == ["codes"] and method == "GET":
            errs = check_query(q, ("link_request_id",))
            lrid = (q.get("link_request_id") or [None])[0]
            if not lrid:
                errs.append(_err("link_request_id", "required"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            with st.lock:
                rec = st.otps.get(lrid)
            if not rec or rec["code"] is None:
                raise ApiError(404, "not_found")
            return self._send(
                200,
                {
                    "link_request_id": lrid,
                    "farmer_id": rec["farmer_id"],
                    "phone": rec["phone"],
                    "code": rec["code"],
                    "expires_at": fmt_time(rec["expires_at"]),
                    "used": rec["used"],
                },
            )
        if svc == "da" and rest == ["link-codes"] and method == "POST":
            body = self._json(raw)
            errs = check_object(body, ("ref",), ("ref",))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            with st.lock:
                if body["ref"] not in st.people:
                    raise ApiError(404, "not_found")
                alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
                code = "".join(secrets.choice(alphabet) for _ in range(8))
                now = st.clock.now()
                st.link_codes[code] = {"ref": body["ref"], "issued_at": now, "used": False}
            return self._send(
                200, {"code": code, "ref": body["ref"], "expires_at": fmt_time(now + LINK_CODE_TTL)}
            )
        raise ApiError(404, "not_found")

    # -- clock ------------------------------------------------------------------

    def _clock(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        clock = self.state.clock
        if rest == [] and method == "GET":
            return self._send(200, {"now": fmt_time(clock.now()), "mode": clock.mode})
        if rest == ["advance"] and method == "POST":
            body = self._json(raw)
            errs = check_object(body, ("seconds",), ("seconds",))
            n = body.get("seconds") if isinstance(body, dict) else None
            if not errs and (
                isinstance(n, bool)
                or not isinstance(n, (int, float))
                or n < 0
                or not math.isfinite(n)
            ):
                errs = [_err("seconds", "must_be_number_at_least_0")]
            if errs:
                raise ApiError(422, "validation_failed", errs)
            return self._send(200, {"now": fmt_time(clock.advance(n))})
        if rest == ["set"] and method == "POST":
            body = self._json(raw)
            errs = check_object(body, ("now",), ("now",))
            when = parse_time(body.get("now")) if not errs else None
            if not errs and when is None:
                errs = [_err("now", "must_be_rfc3339_with_offset")]
            if errs:
                raise ApiError(422, "validation_failed", errs)
            return self._send(200, {"now": fmt_time(clock.set(when))})
        raise ApiError(404, "not_found")

    # -- auth -------------------------------------------------------------------

    def _token(self, raw: bytes) -> None:
        st = self.state
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._send(400, {"error": "invalid_request"})
        form = {
            k: v[0]
            for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()
        }
        if form.get("grant_type") != "client_credentials":
            return self._send(400, {"error": "unsupported_grant_type"})
        ok_id = hmac.compare_digest(form.get("client_id", "").encode(), st.client_id.encode())
        ok_secret = hmac.compare_digest(
            form.get("client_secret", "").encode(), st.client_secret.encode()
        )
        if not (ok_id and ok_secret):
            return self._send(401, {"error": "invalid_client"})
        token = secrets.token_urlsafe(32)
        with st.lock:
            now = time.monotonic()
            st.tokens = {t: e for t, e in st.tokens.items() if e > now}
            st.tokens[token] = now + TOKEN_TTL
        return self._send(
            200,
            {"access_token": token, "expires_in": TOKEN_TTL, "token_type": "Bearer"},
            headers={"Cache-Control": "no-store"},
        )

    def _svc_auth(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        if rest in (["link-otp"], ["link-otp", "verify"]) and method != "POST":
            raise ApiError(405, "method_not_allowed")
        if rest == ["link-otp"]:
            return self._issue_otp(self._json(raw))
        if rest == ["link-otp", "verify"]:
            return self._verify_otp(self._json(raw))
        raise ApiError(404, "not_found")

    def _issue_otp(self, body) -> None:
        st = self.state
        errs = check_object(
            body,
            ("link_request_id", "farmer_id", "phone"),
            ("link_request_id", "farmer_id", "phone"),
        )
        if not errs:
            if not is_uuid(body["link_request_id"]):
                errs.append(_err("link_request_id", "must_be_uuid"))
            if not isinstance(body["farmer_id"], str) or not body["farmer_id"]:
                errs.append(_err("farmer_id", "must_be_string"))
            if not isinstance(body["phone"], str) or not ET_PHONE.match(body["phone"]):
                errs.append(_err("phone", "pattern"))
        if errs:
            raise ApiError(422, "validation_failed", errs)
        lrid = body["link_request_id"]
        with st.lock:
            known = st.otps.get(lrid)
            if known:  # a repeat returns the first answer and sends nothing
                return self._send(200, known["answer"])
            now = st.clock.now()
            recent = [t for t in st.otp_issues.get(body["farmer_id"], []) if now - t < OTP_WINDOW]
            if len(recent) >= OTP_LIMIT:
                answer = {"result": "rate_limited"}
                code = None
            else:
                recent.append(now)
                answer = {"result": "sent", "expires_in": OTP_TTL}
                code = f"{secrets.randbelow(10**6):06d}"
            st.otp_issues[body["farmer_id"]] = recent
            st.otps[lrid] = {
                "farmer_id": body["farmer_id"],
                "phone": body["phone"],
                "code": code,
                "expires_at": now + timedelta(seconds=OTP_TTL),
                "used": False,
                "answer": answer,
            }
        return self._send(200, answer)

    def _verify_otp(self, body) -> None:
        st = self.state
        errs = check_object(body, ("link_request_id", "code"), ("link_request_id", "code"))
        if not errs:
            if not is_uuid(body["link_request_id"]):
                errs.append(_err("link_request_id", "must_be_uuid"))
            if not isinstance(body["code"], str) or not OTP_CODE.match(body["code"]):
                errs.append(_err("code", "pattern"))
        if errs:
            raise ApiError(422, "validation_failed", errs)
        with st.lock:
            rec = st.otps.get(body["link_request_id"])
            if not rec or rec["code"] is None or rec["used"]:
                result = "invalid"
            elif st.clock.now() >= rec["expires_at"]:
                result = "expired"
            elif not hmac.compare_digest(rec["code"].encode(), body["code"].encode()):
                result = "invalid"
            else:
                rec["used"] = True
                result = "valid"
        return self._send(200, {"result": result})

    # -- DA backend -------------------------------------------------------------

    @staticmethod
    def _person(p: dict) -> dict:
        out = {
            "ref": p["ref"],
            "role": p["role"],
            "display_name": p["display_name"],
            "telegram_linked": p.get("telegram_id") is not None,
        }
        if p.get("specialisation"):
            out["specialisation"] = p["specialisation"]
        if p.get("telegram_id") is not None:
            out["telegram_id"] = p["telegram_id"]
        return out

    def _svc_da(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        st = self.state
        if rest == ["roster"] and method == "POST":
            body = self._json(raw)
            errs = check_object(body, ("kebele_codes",), ("kebele_codes",))
            codes = body.get("kebele_codes") if not errs else None
            if not errs and (not isinstance(codes, list) or not 1 <= len(codes) <= 200):
                errs.append(_err("kebele_codes", "must_be_list_of_1_to_200"))
            if not errs:
                errs += [
                    _err(f"kebele_codes[{i}]", "pattern")
                    for i, c in enumerate(codes)
                    if not isinstance(c, str) or not AREA_CODE.match(c)
                ]
            if errs:
                raise ApiError(422, "validation_failed", errs)
            modes = st.seed.get("kebele_routing_modes", {})
            with st.lock:
                out = []
                for code in codes:
                    agents = sorted(
                        (
                            p
                            for p in st.people.values()
                            if p["role"] == "development_agent"
                            and p.get("kebele_code") == code
                            and p.get("posting_effective")
                            and p.get("grant_active")
                        ),
                        key=lambda p: p["ref"],
                    )
                    out.append(
                        {
                            "kebele_code": code,
                            "routing_mode": modes.get(code, "specialism"),
                            "agents": [self._person(p) for p in agents],
                        }
                    )
            return self._send(200, {"kebeles": out})

        if rest == ["staff-links"] and method == "POST":
            body = self._json(raw)
            errs = check_object(body, ("code", "telegram_id"), ("code", "telegram_id"))
            if not errs:
                if not isinstance(body["code"], str) or not LINK_CODE.match(body["code"]):
                    errs.append(_err("code", "pattern"))
                tid = body["telegram_id"]
                if isinstance(tid, bool) or not isinstance(tid, int):
                    errs.append(_err("telegram_id", "must_be_integer"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            with st.lock:
                rec = st.link_codes.get(body["code"])
                if not rec or rec["used"]:
                    return self._send(200, {"result": "invalid"})
                if st.clock.now() >= rec["issued_at"] + LINK_CODE_TTL:
                    return self._send(200, {"result": "expired"})
                rec["used"] = True
                for p in st.people.values():  # one chat belongs to one person
                    if p.get("telegram_id") == body["telegram_id"]:
                        p["telegram_id"] = None
                person = st.people[rec["ref"]]
                person["telegram_id"] = body["telegram_id"]  # a new link replaces the old one
                return self._send(
                    200,
                    {
                        "result": "linked",
                        "ref": person["ref"],
                        "role": person["role"],
                        "display_name": person["display_name"],
                    },
                )

        if (
            len(rest) == 3
            and rest[0] == "staff-links"
            and rest[2] == "unreachable"
            and method == "POST"
        ):
            body = self._json(raw)
            errs = check_object(body, ("reason",), ("reason",))
            if not errs and body["reason"] not in ("blocked", "chat_deleted"):
                errs.append(_err("reason", "enum"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            with st.lock:
                person = st.people.get(rest[1])
                if not person:
                    raise ApiError(404, "not_found")
                person["telegram_id"] = None
            return self._send(204, None)

        if rest == ["officers"] and method == "GET":
            errs = check_query(q, ("level", "code"))
            level = (q.get("level") or [None])[0]
            code = (q.get("code") or [None])[0]
            if level not in ("woreda", "zone", "region"):
                errs.append(_err("level", "enum"))
            if not code or not AREA_CODE.match(code):
                errs.append(_err("code", "pattern"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            role = "supervisor" if level == "woreda" else "executive"
            with st.lock:
                found = sorted(
                    (
                        p
                        for p in st.people.values()
                        if p["role"] == role
                        and p.get("scope_level") == level
                        and p.get("scope_code") == code
                    ),
                    key=lambda p: p["ref"],
                )
                return self._send(200, {"officers": [self._person(p) for p in found]})
        raise ApiError(404, "not_found")

    # -- beneficiary API --------------------------------------------------------

    @staticmethod
    def _farmer(f: dict) -> dict:
        return dict(f)

    def _svc_beneficiary(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        st = self.state
        if method != "GET" or rest[:3] != ["api", "v1", "farmers"] or len(rest) not in (3, 4):
            raise ApiError(404, "not_found")
        if len(rest) == 4:
            if q:
                raise ApiError(400, "validation_failed", [_err(k, "unknown_field") for k in q])
            for f in st.farmers:
                if f["farmer_id"] == rest[3]:
                    return self._send(200, self._farmer(f))
            raise ApiError(404, "not_found")
        allowed = ("level", "code", "record_status", "updated_since", "page_size", "cursor")
        errs = check_query(q, allowed)
        v = {k: q[k][0] for k in allowed if k in q}
        if v.get("level") not in LEVELS:
            errs.append(_err("level", "enum"))
        if not v.get("code") or not BENEFICIARY_CODE.match(v["code"]):
            errs.append(_err("code", "pattern"))
        status = v.get("record_status", "ACTIVE")
        if status not in ("ACTIVE", "INACTIVE", "ARCHIVED"):
            errs.append(_err("record_status", "enum"))
        since = None
        if "updated_since" in v:
            since = parse_time(v["updated_since"])
            if since is None:
                errs.append(_err("updated_since", "invalid_format"))
        try:
            size = int(v.get("page_size", "500"))
            if not 1 <= size <= 1000:
                errs.append(_err("page_size", "out_of_range"))
        except ValueError:
            size = 0
            errs.append(_err("page_size", "invalid_format"))
        try:
            start = int(v.get("cursor", "0"))
            if start < 0:
                raise ValueError
        except ValueError:
            start = 0
            errs.append(_err("cursor", "invalid_format"))
        if errs:
            raise ApiError(400, "validation_failed", errs)
        rows = [
            f
            for f in st.farmers
            if f.get(f"{v['level']}_code") == v["code"]
            and f["record_status"] == status
            and (since is None or parse_time(f["approved_at"]) >= since)
        ]
        chunk = rows[start : start + size]
        nxt = str(start + size) if start + size < len(rows) else None
        return self._send(200, {"items": [self._farmer(f) for f in chunk], "next_cursor": nxt})

    # -- Master Data ------------------------------------------------------------

    @staticmethod
    def _area(a: dict) -> dict:
        return {k: a[k] for k in ("area_code", "name_am", "name_en", "level", "parent_code")}

    def _svc_master_data(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        st = self.state
        if method != "GET" or not rest or rest[0] != "areas" or len(rest) > 2:
            raise ApiError(404, "not_found")
        if len(rest) == 2:
            if q:
                raise ApiError(422, "validation_failed", [_err(k, "unknown_field") for k in q])
            a = st.areas.get(rest[1])
            if not a:
                raise ApiError(404, "not_found")
            parents, p = [], a["parent_code"]
            while p:
                parents.append(self._area(st.areas[p]))
                p = st.areas[p]["parent_code"]
            parents.reverse()  # region first, then down to the direct parent
            return self._send(200, {**self._area(a), "parents": parents})
        errs = check_query(q, ("level", "parent_code"))
        level = (q.get("level") or [None])[0]
        parent = (q.get("parent_code") or [None])[0]
        if level not in LEVELS:
            errs.append(_err("level", "enum"))
        elif level == "region" and parent is not None:
            errs.append(_err("parent_code", "not_allowed_for_region"))
        elif level != "region" and not parent:
            errs.append(_err("parent_code", "required"))
        if errs:
            raise ApiError(422, "validation_failed", errs)
        items = [
            self._area(a)
            for a in st.areas.values()
            if a["level"] == level and a["parent_code"] == parent
        ]
        items.sort(key=lambda a: (a["name_en"], a["area_code"]))
        return self._send(200, {"items": items})

    # -- Marketplace ------------------------------------------------------------

    def _svc_marketplace(self, method: str, rest: list, q: dict, raw: bytes) -> None:
        st = self.state
        if method != "GET":
            raise ApiError(404, "not_found")
        if rest == ["commodities"]:
            errs = check_query(q, ())
            if errs:
                raise ApiError(422, "validation_failed", errs)
            items = [
                {k: c[k] for k in ("commodity_id", "name_am", "name_en", "unit")}
                for c in st.commodities
            ]
            return self._send(200, {"items": items})
        if rest == ["markets"]:
            errs = check_query(q, ("woreda_code", "lat", "lon"))
            woreda = (q.get("woreda_code") or [None])[0]
            lat = lon = None
            if ("lat" in q) != ("lon" in q):
                errs.append(_err("lat" if "lat" not in q else "lon", "required_with_the_other"))
            elif "lat" in q:
                try:
                    lat, lon = float(q["lat"][0]), float(q["lon"][0])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError
                except ValueError:
                    errs.append(_err("lat", "invalid_number"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            markets = list(st.markets)
            if lat is not None:
                markets.sort(key=lambda m: _distance_km(lat, lon, m["lat"], m["lon"]))
            elif woreda:
                same = [m for m in markets if m["woreda_code"] == woreda]
                zone = st.areas.get(woreda, {}).get("parent_code")
                near = [
                    m
                    for m in markets
                    if m["woreda_code"] != woreda
                    and zone
                    and st.areas[m["woreda_code"]]["parent_code"] == zone
                ]
                markets = same + near
            out = [
                {k: m[k] for k in ("market_id", "name_am", "name_en", "woreda_code")}
                for m in markets
            ]
            return self._send(200, {"items": out})
        if rest == ["prices", "latest"]:
            errs = [_err(k, "unknown_field") for k in q if k not in ("commodity_id", "market_id")]
            cid = (q.get("commodity_id") or [None])[0]
            mids = q.get("market_id") or []
            if not cid or len(q.get("commodity_id", [])) > 1:
                errs.append(_err("commodity_id", "required"))
            if not mids:
                errs.append(_err("market_id", "required"))
            elif len(mids) > 5:
                errs.append(_err("market_id", "at_most_5"))
            if errs:
                raise ApiError(422, "validation_failed", errs)
            com = next((c for c in st.commodities if c["commodity_id"] == cid), None)
            today = st.clock.now().astimezone(EAT).date()
            items = []
            if com:
                by_id = {m["market_id"]: m for m in st.markets}
                for mid in dict.fromkeys(mids):
                    m = by_id.get(mid)
                    if not m or (cid, mid) in st.missing_prices:
                        continue
                    items.append(
                        {
                            "market_id": mid,
                            "price_etb": round(com["base_price_etb"] * m["price_factor"], 2),
                            "unit": com["unit"],
                            "price_date": (today - timedelta(days=m["price_age_days"])).isoformat(),
                            "source": "NMIS",
                        }
                    )
            return self._send(200, {"items": items})
        raise ApiError(404, "not_found")


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------


def load_seed(path: Path | str = HERE / "seed.json") -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def make_server(host: str, port: int, state: State) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list | None = None) -> None:
    ap = argparse.ArgumentParser(description="Telegram Phase A stubs and fake clock")
    ap.add_argument("--host", default=os.environ.get("OAN_STUBS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("OAN_STUBS_PORT", "8200")))
    ap.add_argument("--seed", default=os.environ.get("OAN_STUBS_SEED", str(HERE / "seed.json")))
    ap.add_argument(
        "--clock",
        choices=("running", "frozen"),
        default=os.environ.get("OAN_STUBS_CLOCK", "running"),
    )
    args = ap.parse_args(argv)
    client_id = os.environ.get("OAN_STUBS_CLIENT_ID", "")
    client_secret = os.environ.get("OAN_STUBS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        sys.exit("OAN_STUBS_CLIENT_ID and OAN_STUBS_CLIENT_SECRET must be set")
    state = State(load_seed(args.seed), client_id, client_secret, Clock(args.clock))
    server = make_server(args.host, args.port, state)
    print(f"stubs listening on {args.host}:{args.port} (clock {args.clock})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
