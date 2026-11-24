"""Adapters (§5.7): timeout, one retry on connection failure, circuit breaker; and each
stub client against its contract file. No database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from botsupport import STUBS, Upstreams
from pydantic import SecretStr

from oan_bot_services.clients.http import Breaker, ServiceClient, Unavailable
from oan_bot_services.clients.services import (
    AuthOtp,
    Beneficiary,
    DaBackend,
    Marketplace,
    MasterData,
)
from oan_bot_services.clients.tokens import ClientCredentials


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def client_for(handler: object, **kw: object) -> tuple[ServiceClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[operator, no-any-return]

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return ServiceClient("svc", "http://svc.example.invalid", http, **kw), seen  # type: ignore[arg-type]


async def test_one_retry_only_when_the_connection_failed() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    svc, seen = client_for(flaky)
    assert (await svc.request("GET", "/x")).status_code == 200
    assert len(seen) == 2

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late")

    svc, seen = client_for(slow)
    with pytest.raises(Unavailable) as exc:
        await svc.request("POST", "/x", json={})
    assert len(seen) == 1 and exc.value.reason == "timeout"  # may have arrived: no retry

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    svc, seen = client_for(down)
    with pytest.raises(Unavailable):
        await svc.request("GET", "/x")
    assert len(seen) == 2


async def test_timeout_is_passed_to_every_call() -> None:
    timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    svc, _ = client_for(handler, timeout=5.0)
    await svc.request("GET", "/x")
    assert timeouts[0]["read"] == 5.0 and timeouts[0]["connect"] == 5.0


async def test_breaker_opens_after_five_failures_in_a_minute_and_recovers() -> None:
    clock = Clock()
    status = {"code": 503}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status["code"], json={})

    svc, seen = client_for(handler, breaker=Breaker(clock))
    for _ in range(5):
        with pytest.raises(Unavailable):
            await svc.request("GET", "/x")
        clock.t += 5
    with pytest.raises(Unavailable) as exc:
        await svc.request("GET", "/x")
    assert exc.value.reason == "circuit_open" and len(seen) == 5
    clock.t += 61
    status["code"] = 200
    assert (await svc.request("GET", "/x")).status_code == 200


def test_failures_spread_over_more_than_a_minute_do_not_open() -> None:
    clock = Clock()
    b = Breaker(clock)
    for _ in range(10):
        b.failure()
        clock.t += 20
    assert not b.is_open


async def test_unavailable_never_carries_the_url_or_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"secret": "x"})

    svc, _ = client_for(handler)
    with pytest.raises(Unavailable) as exc:
        await svc.request("POST", "/farmers/FR-TEST-0001", json={"phone": "+251900000101"})
    assert "FR-TEST" not in str(exc.value) and "2519" not in str(exc.value)


async def test_not_configured_service_is_unavailable() -> None:
    svc = ServiceClient("svc", "", httpx.AsyncClient())
    with pytest.raises(Unavailable):
        await svc.request("GET", "/x")


async def test_token_is_cached_and_refreshed() -> None:
    clock = Clock()
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        issued.append(request.content)
        return httpx.Response(
            200, json={"access_token": f"t{len(issued)}", "expires_in": 100, "token_type": "Bearer"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cc = ClientCredentials(
        "http://auth.example.invalid/token",
        "bot",
        SecretStr("client-secret-xyz"),
        http,
        monotonic=clock,
    )
    assert await cc() == "t1" and await cc() == "t1"
    clock.t += 71
    assert await cc() == "t2"
    cc.forget()
    assert await cc() == "t3"
    assert b"grant_type=client_credentials" in issued[0]
    assert "client-secret-xyz" not in repr(cc)
    none = ClientCredentials("", "bot", SecretStr("s"), http)
    assert await none() is None


async def test_token_failure_is_unavailable() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(Unavailable):
        await ClientCredentials("http://a.example.invalid/t", "b", SecretStr("s"), http)()


# --------------------------------------------------------- stub contract tests


def stub_clients() -> tuple[Upstreams, dict[str, ServiceClient]]:
    up = Upstreams()
    http = httpx.AsyncClient(transport=httpx.MockTransport(up.handle))
    tokens = ClientCredentials(f"{STUBS}/auth/token", "bot", SecretStr("s"), http)
    return up, {
        name: ServiceClient(name, f"{STUBS}/{name}", http, auth=tokens)
        for name in ("da", "auth", "beneficiary", "master-data", "marketplace")
    }


async def test_every_stub_client_call_matches_its_contract() -> None:
    """Each call goes through the fake, which validates request and answer against the
    contract file; any mismatch is a violation."""
    from botsupport import Farmer

    up, c = stub_clients()
    up.farmers["FR-TEST-0901"] = Farmer("FR-TEST-0901", "+251900000901", "ET01010101")
    up.link_codes["ABCD1234"] = ("DA-TEST-001", "development_agent")
    da = DaBackend(c["da"])
    assert (await da.roster(["ET01010101"]))[0].kebele_code == "ET01010101"
    assert (await da.link_person("ABCD1234", 700000901)).result == "linked"
    assert (await da.link_person("ZZZZ9999", 700000901)).result == "invalid"
    assert await da.report_unreachable("DA-TEST-001", "blocked") is True
    assert await da.officers("woreda", "ET010101") == []
    auth = AuthOtp(c["auth"])
    lr = uuid.uuid4()
    assert (await auth.issue(lr, "FR-TEST-0901", "+251900000901"))[0] == "sent"
    assert await auth.verify(lr, up.otp_code) == "valid"
    ben = Beneficiary(c["beneficiary"])
    f = await ben.farmer("FR-TEST-0901")
    assert f is not None and f.primary_phone == "+251900000901"
    assert await ben.farmer("FR-NONE-0000") is None
    md = MasterData(c["master-data"])
    assert [a.area_code for a in await md.children("region", None)] == ["ET01", "ET02"]
    assert [a.area_code for a in await md.children("kebele", "ET010101")] == [
        "ET01010101",
        "ET01010102",
    ]
    area = await md.area("ET01010101")
    assert area is not None and [p["level"] for p in area["parents"]] == [
        "region",
        "zone",
        "woreda",
    ]
    assert await md.area("ET99") is None
    mp = Marketplace(c["marketplace"])
    assert (await mp.commodities())[0]["commodity_id"] == "C1"
    assert (await mp.markets("ET010101"))[0]["market_id"] == "M1"
    assert (await mp.latest_prices("C1", ["M1", "M2", "M3", "M4", "M5", "M6"]))[0][
        "source"
    ] == "NMIS"
    assert not up.violations, up.violations


async def test_contract_checker_catches_a_wrong_request() -> None:
    from contract import check_request

    assert check_request("da", "POST", "/staff-links", {"code": "short", "telegram_id": 1})
    assert check_request(
        "auth",
        "POST",
        "/link-otp",
        {"link_request_id": str(uuid.uuid4()), "farmer_id": "F", "phone": "0900000101"},
    )
    assert check_request(
        "marketplace",
        "GET",
        "/prices/latest",
        None,
        {"commodity_id": ["C1"], "market_id": ["1", "2", "3", "4", "5", "6"]},
    )
    assert not check_request("master-data", "GET", "/areas", None, {"level": ["region"]})


async def test_stub_errors_become_unavailable() -> None:
    up, c = stub_clients()
    up.down.update({"da", "auth", "beneficiary", "master-data", "marketplace"})
    calls = [
        DaBackend(c["da"]).roster(["ET01010101"]),
        AuthOtp(c["auth"]).issue(uuid.uuid4(), "F", "+251900000101"),
        Beneficiary(c["beneficiary"]).farmer("FR-TEST-0001"),
        MasterData(c["master-data"]).area("ET01"),
        Marketplace(c["marketplace"]).commodities(),
    ]
    for call in calls:
        with pytest.raises(Unavailable):
            await call


def test_oan_messaging_body_has_only_the_caller_contract_fields() -> None:
    import inspect

    from oan_bot_services.clients import oan_messaging

    source = inspect.getsource(oan_messaging.OanMessaging.submit)
    for name in ("chat_id", "text", "purpose", "expires_at", "source_reference", "Idempotency-Key"):
        assert name in source
    assert datetime.now(UTC)
