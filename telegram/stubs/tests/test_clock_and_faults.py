"""The fake clock (stubs/clock.md) and the fault switches."""

from __future__ import annotations

import time
import uuid

import pytest
import stubs_server

# ---------------------------------------------------------------------------
# Clock


def test_clock_starts_at_reset_time(stub):
    assert stub.raw("GET", "/clock").json() == {"now": "2026-12-01T08:30:00+03:00"}


def test_clock_advance_and_set(stub):
    r = stub.raw("POST", "/clock/advance", json={"seconds": 90})
    assert r.json() == {"now": "2026-12-01T08:31:30+03:00"}
    assert (
        stub.raw("POST", "/clock/advance", json={"seconds": 0}).json()["now"]
        == "2026-12-01T08:31:30+03:00"
    )
    r = stub.raw("POST", "/clock/set", json={"now": "2026-12-03T06:00:00Z"})
    assert r.json() == {"now": "2026-12-03T09:00:00+03:00"}
    assert stub.raw("GET", "/clock").json()["now"] == "2026-12-03T09:00:00+03:00"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/clock/advance", {"seconds": -1}),
        ("/clock/advance", {"seconds": "10"}),
        ("/clock/advance", {"seconds": True}),
        ("/clock/advance", {"seconds": 1, "extra": 1}),
        ("/clock/advance", {}),
        ("/clock/set", {"now": "2026-11-30T08:00:00+03:00"}),  # earlier: never backwards
        ("/clock/set", {"now": "2026-12-05T08:00:00"}),  # no offset
        ("/clock/set", {"now": "tomorrow"}),
    ],
)
def test_clock_refuses(stub, path, body):
    assert stub.raw("POST", path, json=body).status_code == 422
    assert stub.raw("GET", "/clock").json()["now"] == "2026-12-01T08:30:00+03:00"


def test_clock_needs_no_token(stub):
    assert stub.raw("GET", "/clock").status_code == 200


def test_ticking_clock_moves_with_real_time():
    clock = stubs_server.Clock("ticking")
    a = clock.now()
    time.sleep(0.05)
    b = clock.now()
    assert b > a
    clock.advance(60)
    assert clock.now() - b >= stubs_server.timedelta(seconds=60)
    with pytest.raises(stubs_server.ApiError):
        clock.set(a)


def test_frozen_clock_stands_still():
    clock = stubs_server.Clock("frozen")
    a = clock.now()
    time.sleep(0.02)
    assert clock.now() == a


# ---------------------------------------------------------------------------
# Fault switches

CALLS = {
    "da": ("GET", "/da/officers?level=woreda&code=ET910101", {}),
    "auth": (
        "POST",
        "/auth/link-otp/verify",
        {"json": {"link_request_id": str(uuid.uuid4()), "code": "123456"}},
    ),
    "beneficiary": ("GET", "/beneficiary/api/v1/farmers/FR-TEST-0001", {}),
    "master-data": ("GET", "/master-data/areas?level=region", {}),
    "marketplace": ("GET", "/marketplace/commodities", {}),
}


@pytest.mark.parametrize("svc", list(CALLS))
def test_down_switch_gives_503_for_that_service_only(stub, svc):
    method, path, kw = CALLS[svc]
    r = stub.raw("POST", f"/{svc}/_test/down", json={"on": True})
    assert r.json() == {"service": svc, "down": True, "slow": False}
    assert stub.call(method, path, **kw).status_code == 503
    for other, (m, p, k) in CALLS.items():
        if other != svc:
            assert stub.call(m, p, **k).status_code == 200, other
    stub.raw("POST", f"/{svc}/_test/down", json={"on": False})
    assert stub.call(method, path, **kw).status_code == 200


def test_down_needs_no_token_and_comes_before_auth(stub):
    stub.raw("POST", "/marketplace/_test/down")  # empty body means on
    assert stub.raw("GET", "/marketplace/commodities").status_code == 503


def test_token_route_still_works_while_auth_is_down(stub):
    stub.raw("POST", "/auth/_test/down", json={"on": True})
    stub._token = None
    assert stub.token()
    assert (
        stub.call(
            "POST",
            "/auth/link-otp/verify",
            json={"link_request_id": str(uuid.uuid4()), "code": "123456"},
        ).status_code
        == 503
    )


def test_switch_validation(stub):
    assert stub.raw("POST", "/da/_test/down", json={"on": "yes"}).status_code == 422
    assert stub.raw("POST", "/da/_test/slow", json={"on": True, "x": 1}).status_code == 422
    assert stub.raw("POST", "/nothing/_test/down").status_code == 404


def test_slow_switch_delays_six_seconds(stub):
    assert stub.state.slow_seconds == 6.0
    stub.raw("POST", "/marketplace/_test/slow", json={"on": True})
    start = time.monotonic()
    r = stub.call("GET", "/marketplace/commodities")
    took = time.monotonic() - start
    assert r.status_code == 200
    assert 6.0 <= took < 8.0
    # other services are not slowed
    start = time.monotonic()
    stub.call("GET", "/master-data/areas?level=region")
    assert time.monotonic() - start < 1.0


def test_reset_clears_everything(stub):
    stub.raw("POST", "/da/_test/down")
    stub.raw("POST", "/master-data/_test/slow")
    stub.raw("POST", "/clock/advance", json={"seconds": 3600})
    lrid = str(uuid.uuid4())
    stub.call(
        "POST",
        "/auth/link-otp",
        json={"link_request_id": lrid, "farmer_id": "FR-TEST-0001", "phone": "+251900000001"},
    )
    r = stub.raw("POST", "/_test/reset")
    assert r.json() == {"ok": True, "now": "2026-12-01T08:30:00+03:00"}
    assert stub.state.down["da"] is False and stub.state.slow["master-data"] is False
    assert stub.raw("GET", f"/auth/_test/codes?link_request_id={lrid}").status_code == 404
    # link state back to the seed
    stub.call("POST", "/da/staff-links/DA-TEST-001/unreachable", json={"reason": "blocked"})
    stub.raw("POST", "/_test/reset")
    r = stub.call("POST", "/da/roster", json={"kebele_codes": ["ET91010101"]})
    assert r.json()["kebeles"][0]["agents"][0]["telegram_id"] == 900000101
    # tokens survive a reset, so a running Bot Services keeps working
    assert stub.call("GET", "/master-data/areas?level=region").status_code == 200
