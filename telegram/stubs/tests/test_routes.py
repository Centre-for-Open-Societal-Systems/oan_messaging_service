"""Every stub route, checked against its contract file (see conftest.StubClient)."""

from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# Token and 401


def test_token_issued_for_the_test_client(stub):
    r = stub.call(
        "POST",
        "/auth/token",
        auth=False,
        data={
            "grant_type": "client_credentials",
            "client_id": stub.client_id,
            "client_secret": stub.client_secret,
            "scope": "openid",
        },
    )
    assert r.status_code == 200
    assert r.json()["token_type"] == "Bearer"  # noqa: S105
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "form,status",
    [
        (
            {
                "grant_type": "client_credentials",
                "client_id": "test-client",
                "client_secret": "wrong",
            },
            401,
        ),
        ({"grant_type": "client_credentials", "client_id": "other", "client_secret": "wrong"}, 401),
        ({"grant_type": "password", "client_id": "test-client", "client_secret": "x"}, 400),
    ],
)
def test_token_refused(stub, form, status):
    assert stub.call("POST", "/auth/token", auth=False, data=form).status_code == status


def test_token_needs_form_encoding(stub):
    r = stub.raw("POST", "/auth/token", json={"grant_type": "client_credentials"})
    assert r.status_code == 400


PROTECTED = [
    ("POST", "/da/roster", {"json": {"kebele_codes": ["ET91010101"]}}),
    ("POST", "/da/staff-links", {"json": {"code": "TESTDA03", "telegram_id": 900000999}}),
    ("POST", "/da/staff-links/DA-TEST-001/unreachable", {"json": {"reason": "blocked"}}),
    ("GET", "/da/officers?level=woreda&code=ET910101", {}),
    (
        "POST",
        "/auth/link-otp",
        {
            "json": {
                "link_request_id": str(uuid.uuid4()),
                "farmer_id": "FR-TEST-0001",
                "phone": "+251900000001",
            }
        },
    ),
    (
        "POST",
        "/auth/link-otp/verify",
        {"json": {"link_request_id": str(uuid.uuid4()), "code": "123456"}},
    ),
    ("GET", "/beneficiary/api/v1/farmers?level=kebele&code=ET91010101", {}),
    ("GET", "/beneficiary/api/v1/farmers/FR-TEST-0001", {}),
    ("GET", "/master-data/areas?level=region", {}),
    ("GET", "/master-data/areas/ET91", {}),
    ("GET", "/marketplace/commodities", {}),
    ("GET", "/marketplace/markets", {}),
    ("GET", "/marketplace/prices/latest?commodity_id=teff&market_id=MKT-ET910101-1", {}),
]


@pytest.mark.parametrize("method,path,kw", PROTECTED, ids=[p[1].split("?")[0] for p in PROTECTED])
@pytest.mark.parametrize("header", [None, "Bearer not-a-token", "Basic abc"])
def test_missing_or_wrong_token_gives_401(stub, method, path, kw, header):
    headers = {"Authorization": header} if header else {}
    r = stub.call(method, path, auth=False, headers=headers, **kw)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# Unknown fields give 422 (400 for the beneficiary API, whose contract says 400)

UNKNOWN_BODY = [
    ("/da/roster", {"kebele_codes": ["ET91010101"], "extra": 1}, "extra"),
    ("/da/staff-links", {"code": "TESTDA03", "telegram_id": 1, "phone": "+251900000001"}, "phone"),
    ("/da/staff-links/DA-TEST-001/unreachable", {"reason": "blocked", "note": "x"}, "note"),
    (
        "/auth/link-otp",
        {
            "link_request_id": str(uuid.uuid4()),
            "farmer_id": "FR-TEST-0001",
            "phone": "+251900000001",
            "name": "x",
        },
        "name",
    ),
    (
        "/auth/link-otp/verify",
        {"link_request_id": str(uuid.uuid4()), "code": "123456", "chat": 1},
        "chat",
    ),
]


@pytest.mark.parametrize("path,body,field", UNKNOWN_BODY, ids=[p for p, _, _ in UNKNOWN_BODY])
def test_unknown_body_field_gives_422(stub, path, body, field):
    r = stub.call("POST", path, json=body, check_request=False)
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": field, "code": "unknown_field"}]


@pytest.mark.parametrize(
    "path",
    [
        "/da/officers?level=woreda&code=ET910101&phone=1",
        "/master-data/areas?level=region&lang=am",
        "/master-data/areas/ET91?x=1",
        "/marketplace/commodities?lang=am",
        "/marketplace/markets?farmer=1",
        "/marketplace/prices/latest?commodity_id=teff&market_id=MKT-ET910101-1&x=1",
    ],
)
def test_unknown_query_field_gives_422(stub, path):
    assert stub.call("GET", path).status_code == 422


def test_beneficiary_unknown_query_field_gives_400(stub):
    r = stub.call("GET", "/beneficiary/api/v1/farmers?level=kebele&code=ET91010101&phone=1")
    assert r.status_code == 400
    assert r.json()["code"] == "validation_failed"


def test_bad_json_and_wrong_types_give_422(stub):
    tok = {"Authorization": f"Bearer {stub.token()}", "Content-Type": "application/json"}
    assert stub.raw("POST", "/da/roster", content=b"{not json", headers=tok).status_code == 422
    assert stub.raw("POST", "/da/roster", content=b"[]", headers=tok).status_code == 422
    for body in (
        {"kebele_codes": []},
        {"kebele_codes": ["et-lower"]},
        {"kebele_codes": ["X"] * 201},
    ):
        assert stub.call("POST", "/da/roster", json=body, check_request=False).status_code == 422
    assert (
        stub.call(
            "POST", "/da/staff-links", json={"code": "short", "telegram_id": 1}, check_request=False
        ).status_code
        == 422
    )
    assert (
        stub.call(
            "POST",
            "/da/staff-links",
            json={"code": "TESTDA03", "telegram_id": "1"},
            check_request=False,
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# Master Data


def _names(r):
    return [a["name_en"] for a in r.json()["items"]]


def test_master_data_tree(stub):
    r = stub.call("GET", "/master-data/areas?level=region")
    assert r.status_code == 200
    regions = r.json()["items"]
    assert [a["area_code"] for a in regions] == ["ET91", "ET92"]
    assert all(a["parent_code"] is None for a in regions)
    zones = stub.call("GET", "/master-data/areas?level=zone&parent_code=ET91").json()["items"]
    assert [z["area_code"] for z in zones] == ["ET9101", "ET9102"]
    woredas = stub.call("GET", "/master-data/areas?level=woreda&parent_code=ET9101").json()["items"]
    assert len(woredas) == 2
    r = stub.call("GET", "/master-data/areas?level=kebele&parent_code=ET910101")
    assert _names(r) == ["Kebele 01", "Kebele 02", "Kebele 03"]
    assert all(a["name_am"].startswith("ቀበሌ") for a in r.json()["items"])
    assert (
        stub.call("GET", "/master-data/areas?level=kebele&parent_code=ET999999").json()["items"]
        == []
    )
    # wrong level for the parent gives an empty list, not an error
    assert (
        stub.call("GET", "/master-data/areas?level=kebele&parent_code=ET91").json()["items"] == []
    )


def test_master_data_children_ordered_by_english_name(stub):
    r = stub.call("GET", "/master-data/areas?level=woreda&parent_code=ET9102")
    assert _names(r) == sorted(_names(r))


@pytest.mark.parametrize("q", ["level=zone", "level=region&parent_code=ET91", "level=county", ""])
def test_master_data_bad_list_query(stub, q):
    assert stub.call("GET", f"/master-data/areas?{q}").status_code == 422


def test_master_data_get_area_with_parents(stub):
    r = stub.call("GET", "/master-data/areas/ET91010102")
    assert r.status_code == 200
    a = r.json()
    assert a["level"] == "kebele" and a["parent_code"] == "ET910101"
    assert [p["area_code"] for p in a["parents"]] == ["ET91", "ET9101", "ET910101"]
    assert stub.call("GET", "/master-data/areas/ET91").json()["parents"] == []
    assert stub.call("GET", "/master-data/areas/ET99999999").status_code == 404


# ---------------------------------------------------------------------------
# Beneficiary API


def test_beneficiary_list_by_kebele_and_paging(stub):
    r = stub.call("GET", "/beneficiary/api/v1/farmers?level=woreda&code=ET910101&page_size=2")
    assert r.status_code == 200
    seen, body = [], r.json()
    seen += [f["farmer_id"] for f in body["items"]]
    while body["next_cursor"]:
        body = stub.call(
            "GET",
            f"/beneficiary/api/v1/farmers?level=woreda&code=ET910101&page_size=2"
            f"&cursor={body['next_cursor']}",
        ).json()
        seen += [f["farmer_id"] for f in body["items"]]
    everyone = stub.call("GET", "/beneficiary/api/v1/farmers?level=woreda&code=ET910101").json()
    assert everyone["next_cursor"] is None
    assert seen == [f["farmer_id"] for f in everyone["items"]]
    assert len(seen) >= 5
    assert all(f["woreda_code"] == "ET910101" for f in everyone["items"])


def test_beneficiary_record_status_and_updated_since(stub):
    active = stub.call("GET", "/beneficiary/api/v1/farmers?level=region&code=ET91").json()["items"]
    inactive = stub.call(
        "GET", "/beneficiary/api/v1/farmers?level=region&code=ET91&record_status=INACTIVE"
    )
    assert inactive.status_code == 200
    assert [f["record_status"] for f in inactive.json()["items"]] == ["INACTIVE"]
    recent = stub.call(
        "GET",
        "/beneficiary/api/v1/farmers?level=region&code=ET91"
        "&updated_since=2026-11-15T00:00:00%2B03:00",
    ).json()["items"]
    assert 0 < len(recent) < len(active)


def test_beneficiary_get_one(stub):
    r = stub.call("GET", "/beneficiary/api/v1/farmers/FR-TEST-0001")
    assert r.status_code == 200
    f = r.json()
    assert f["primary_phone"] == "+251900000001" and f["kebele_code"] == "ET91010101"
    no_phone = stub.call("GET", "/beneficiary/api/v1/farmers/FR-TEST-0005").json()
    assert no_phone["primary_phone"] is None and no_phone["phones"] == []
    r = stub.call("GET", "/beneficiary/api/v1/farmers/FR-TEST-9999")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


@pytest.mark.parametrize(
    "q",
    [
        "level=district&code=ET91",
        "level=kebele",
        "level=kebele&code=ET91010101&page_size=0",
        "level=kebele&code=ET91010101&cursor=abc",
        "level=kebele&code=ET91010101&record_status=X",
    ],
)
def test_beneficiary_bad_query_gives_400(stub, q):
    r = stub.call("GET", f"/beneficiary/api/v1/farmers?{q}")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth: linking OTP


def _issue(stub, farmer="FR-TEST-0001", phone="+251900000001", lrid=None):
    lrid = lrid or str(uuid.uuid4())
    r = stub.call(
        "POST",
        "/auth/link-otp",
        json={"link_request_id": lrid, "farmer_id": farmer, "phone": phone},
    )
    assert r.status_code == 200
    return lrid, r.json()


def _code(stub, lrid):
    r = stub.raw("GET", f"/auth/_test/codes?link_request_id={lrid}")
    assert r.status_code == 200
    return r.json()


def _verify(stub, lrid, code):
    r = stub.call("POST", "/auth/link-otp/verify", json={"link_request_id": lrid, "code": code})
    assert r.status_code == 200
    return r.json()["result"]


def test_otp_issue_and_verify_once(stub):
    lrid, ans = _issue(stub)
    assert ans == {"result": "sent", "expires_in": 600}
    rec = _code(stub, lrid)
    assert rec["phone"] == "+251900000001" and rec["farmer_id"] == "FR-TEST-0001"
    assert rec["expires_at"] == "2026-12-01T08:40:00+03:00"
    wrong = "000000" if rec["code"] != "000000" else "111111"
    assert _verify(stub, lrid, wrong) == "invalid"
    assert _verify(stub, lrid, rec["code"]) == "valid"
    assert _verify(stub, lrid, rec["code"]) == "invalid"  # works once
    assert _code(stub, lrid)["used"] is True


def test_otp_unknown_request_is_invalid(stub):
    assert _verify(stub, str(uuid.uuid4()), "123456") == "invalid"


def test_otp_expires_on_the_fake_clock(stub):
    lrid, _ = _issue(stub)
    code = _code(stub, lrid)["code"]
    stub.raw("POST", "/clock/advance", json={"seconds": 599})
    lrid2, _ = _issue(stub, farmer="FR-TEST-0002", phone="+251900000002")
    stub.raw("POST", "/clock/advance", json={"seconds": 1})
    assert _verify(stub, lrid, code) == "expired"
    assert _verify(stub, lrid2, _code(stub, lrid2)["code"]) == "valid"


def test_otp_rate_limit_three_per_farmer_in_15_minutes(stub):
    for _ in range(3):
        assert _issue(stub)[1]["result"] == "sent"
    lrid, ans = _issue(stub)
    assert ans == {"result": "rate_limited"}
    assert stub.raw("GET", f"/auth/_test/codes?link_request_id={lrid}").status_code == 404
    assert _verify(stub, lrid, "123456") == "invalid"
    assert _issue(stub, farmer="FR-TEST-0002", phone="+251900000002")[1]["result"] == "sent"
    stub.raw("POST", "/clock/advance", json={"seconds": 15 * 60})
    assert _issue(stub)[1]["result"] == "sent"


def test_otp_repeat_request_id_returns_first_answer_and_sends_nothing(stub):
    lrid, first = _issue(stub)
    code = _code(stub, lrid)["code"]
    for _ in range(5):
        assert _issue(stub, lrid=lrid)[1] == first
    assert _code(stub, lrid)["code"] == code
    assert _issue(stub)[1]["result"] == "sent"  # repeats did not count against the limit
    assert _issue(stub)[1]["result"] == "sent"
    assert _issue(stub)[1]["result"] == "rate_limited"


@pytest.mark.parametrize(
    "body",
    [
        {"link_request_id": "not-a-uuid", "farmer_id": "FR-TEST-0001", "phone": "+251900000001"},
        {"link_request_id": str(uuid.uuid4()), "farmer_id": "FR-TEST-0001", "phone": "0900000001"},
        {"link_request_id": str(uuid.uuid4()), "farmer_id": "FR-TEST-0001"},
    ],
)
def test_otp_issue_validation(stub, body):
    assert stub.call("POST", "/auth/link-otp", json=body, check_request=False).status_code == 422


def test_test_codes_needs_request_id(stub):
    assert stub.raw("GET", "/auth/_test/codes").status_code == 422


# ---------------------------------------------------------------------------
# DA backend: R1 to R4


def _roster(stub, *codes):
    r = stub.call("POST", "/da/roster", json={"kebele_codes": list(codes)})
    assert r.status_code == 200
    return {k["kebele_code"]: k for k in r.json()["kebeles"]}


def test_r1_roster(stub):
    ros = _roster(
        stub, "ET91010101", "ET91010102", "ET91010103", "ET91010201", "ET92010102", "ET91020101"
    )
    k1 = ros["ET91010101"]
    assert k1["routing_mode"] == "specialism"
    assert [a["ref"] for a in k1["agents"]] == ["DA-TEST-001", "DA-TEST-002", "DA-TEST-003"]
    crop = [a for a in k1["agents"] if a["specialisation"] == "CROP_PRODUCTION"]
    assert len(crop) == 2 and all(a["telegram_linked"] for a in crop)
    unlinked = k1["agents"][2]
    assert unlinked["telegram_linked"] is False and "telegram_id" not in unlinked
    assert ros["ET91010102"]["routing_mode"] == "kebele_inbox"
    assert all(not a["telegram_linked"] for a in ros["ET91010103"]["agents"])
    # no active grant / posting not effective: left out
    assert [a["ref"] for a in ros["ET91010201"]["agents"]] == ["DA-TEST-007"]
    assert ros["ET92010102"]["agents"] == []
    assert ros["ET91020101"]["agents"] == []


def test_r1_unknown_kebele_gives_empty_roster(stub):
    ros = _roster(stub, "ET99999999")
    assert ros == {
        "ET99999999": {"kebele_code": "ET99999999", "routing_mode": "specialism", "agents": []}
    }


def test_r2_link_code_works_once(stub):
    r = stub.call("POST", "/da/staff-links", json={"code": "TESTDA03", "telegram_id": 900000303})
    assert r.json() == {
        "result": "linked",
        "ref": "DA-TEST-003",
        "role": "development_agent",
        "display_name": "Test Agent 03",
    }
    agent = _roster(stub, "ET91010101")["ET91010101"]["agents"][2]
    assert agent["telegram_linked"] is True and agent["telegram_id"] == 900000303
    again = stub.call(
        "POST", "/da/staff-links", json={"code": "TESTDA03", "telegram_id": 900000303}
    )
    assert again.json() == {"result": "invalid"}
    assert stub.call(
        "POST", "/da/staff-links", json={"code": "NOSUCH01", "telegram_id": 1}
    ).json() == {"result": "invalid"}


def test_r2_link_code_expires_after_15_minutes(stub):
    stub.raw("POST", "/clock/advance", json={"seconds": 15 * 60 - 1})
    assert (
        stub.call("POST", "/da/staff-links", json={"code": "TESTDA06", "telegram_id": 5}).json()[
            "result"
        ]
        == "linked"
    )
    stub.raw("POST", "/clock/advance", json={"seconds": 1})
    assert stub.call(
        "POST", "/da/staff-links", json={"code": "TESTSUP2", "telegram_id": 6}
    ).json() == {"result": "expired"}


def test_r2_new_code_from_test_route_and_new_link_replaces_old(stub):
    stub.raw("POST", "/clock/advance", json={"seconds": 3600})
    r = stub.raw("POST", "/da/_test/link-codes", json={"ref": "DA-TEST-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["expires_at"] == "2026-12-01T09:45:00+03:00"
    linked = stub.call(
        "POST", "/da/staff-links", json={"code": body["code"], "telegram_id": 900000777}
    )
    assert linked.json()["result"] == "linked"
    a1 = _roster(stub, "ET91010101")["ET91010101"]["agents"][0]
    assert a1["telegram_id"] == 900000777
    assert stub.raw("POST", "/da/_test/link-codes", json={"ref": "NOBODY"}).status_code == 404


def test_r2_one_chat_belongs_to_one_person(stub):
    stub.call("POST", "/da/staff-links", json={"code": "TESTDA03", "telegram_id": 900000101})
    agents = _roster(stub, "ET91010101")["ET91010101"]["agents"]
    assert agents[0]["telegram_linked"] is False  # DA-TEST-001 had that chat
    assert agents[2]["telegram_id"] == 900000101


def test_r2_supervisor_and_executive_roles(stub):
    assert (
        stub.call("POST", "/da/staff-links", json={"code": "TESTSUP2", "telegram_id": 7}).json()[
            "role"
        ]
        == "supervisor"
    )
    assert (
        stub.call("POST", "/da/staff-links", json={"code": "TESTEXE1", "telegram_id": 8}).json()[
            "role"
        ]
        == "executive"
    )


def test_r3_unreachable_unlinks(stub):
    r = stub.call("POST", "/da/staff-links/DA-TEST-001/unreachable", json={"reason": "blocked"})
    assert r.status_code == 204 and r.content == b""
    a1 = _roster(stub, "ET91010101")["ET91010101"]["agents"][0]
    assert a1["telegram_linked"] is False and "telegram_id" not in a1
    r = stub.call(
        "POST", "/da/staff-links/SUP-TEST-001/unreachable", json={"reason": "chat_deleted"}
    )
    assert r.status_code == 204
    assert (
        stub.call(
            "POST", "/da/staff-links/NOBODY/unreachable", json={"reason": "blocked"}
        ).status_code
        == 404
    )
    assert (
        stub.call(
            "POST",
            "/da/staff-links/DA-TEST-001/unreachable",
            json={"reason": "left"},
            check_request=False,
        ).status_code
        == 422
    )


def test_r4_officers(stub):
    def refs(level, code):
        r = stub.call("GET", f"/da/officers?level={level}&code={code}")
        assert r.status_code == 200
        return [o["ref"] for o in r.json()["officers"]]

    assert refs("woreda", "ET910101") == ["SUP-TEST-001", "SUP-TEST-002"]
    assert refs("woreda", "ET910102") == ["SUP-TEST-003"]
    assert refs("woreda", "ET910201") == []
    assert refs("zone", "ET9101") == ["EXE-TEST-001"]
    assert refs("region", "ET92") == ["EXE-TEST-002"]
    assert refs("region", "ET91") == []
    assert refs("woreda", "ET99999999") == []
    sup = stub.call("GET", "/da/officers?level=woreda&code=ET910101").json()["officers"]
    assert sup[0]["telegram_id"] == 900000201 and sup[1]["telegram_linked"] is False


@pytest.mark.parametrize("q", ["level=kebele&code=ET91010101", "level=woreda", "code=ET91"])
def test_r4_bad_query(stub, q):
    assert stub.call("GET", f"/da/officers?{q}").status_code == 422


# ---------------------------------------------------------------------------
# Marketplace


def test_commodities(stub):
    items = stub.call("GET", "/marketplace/commodities").json()["items"]
    assert len(items) == 10
    assert items[0]["commodity_id"] == "teff"


def test_markets_by_woreda_then_neighbours(stub):
    items = stub.call("GET", "/marketplace/markets?woreda_code=ET910101").json()["items"]
    assert [m["woreda_code"] for m in items] == ["ET910101"] * 2 + ["ET910102"] * 2
    assert stub.call("GET", "/marketplace/markets?woreda_code=ET99999999").json()["items"] == []
    assert len(stub.call("GET", "/marketplace/markets").json()["items"]) == 16


def test_markets_by_distance(stub):
    items = stub.call("GET", "/marketplace/markets?lat=10.75&lon=38.6").json()["items"]
    assert items[0]["woreda_code"] == "ET910202"
    assert len(items) == 16
    assert stub.call("GET", "/marketplace/markets?lat=10.0").status_code == 422
    assert stub.call("GET", "/marketplace/markets?lat=x&lon=1").status_code == 422


def test_prices_dated_by_the_fake_clock(stub):
    mids = ["MKT-ET910101-1", "MKT-ET910101-2", "MKT-ET910102-2", "MKT-NONE"]
    q = "&".join(f"market_id={m}" for m in mids)
    items = stub.call("GET", f"/marketplace/prices/latest?commodity_id=teff&{q}").json()["items"]
    by = {i["market_id"]: i for i in items}
    assert set(by) == {"MKT-ET910101-1", "MKT-ET910101-2", "MKT-ET910102-2"}
    assert by["MKT-ET910101-1"]["price_date"] == "2026-12-01"
    assert by["MKT-ET910102-2"]["price_date"] == "2026-10-22"  # the stale one, 40 days old
    assert by["MKT-ET910101-1"]["unit"] == "quintal" and by["MKT-ET910101-1"]["source"] == "NMIS"
    stub.raw("POST", "/clock/advance", json={"seconds": 86400})
    again = stub.call(
        "GET", "/marketplace/prices/latest?commodity_id=teff&market_id=MKT-ET910101-1"
    )
    assert again.json()["items"][0]["price_date"] == "2026-12-02"


def test_prices_missing_and_unknown(stub):
    r = stub.call("GET", "/marketplace/prices/latest?commodity_id=coffee&market_id=MKT-ET910101-2")
    assert r.json()["items"] == []
    r = stub.call("GET", "/marketplace/prices/latest?commodity_id=gold&market_id=MKT-ET910101-1")
    assert r.json()["items"] == []


@pytest.mark.parametrize(
    "q",
    [
        "market_id=MKT-ET910101-1",
        "commodity_id=teff",
        "commodity_id=teff&" + "&".join(f"market_id=M{i}" for i in range(6)),
    ],
)
def test_prices_bad_query(stub, q):
    assert stub.call("GET", f"/marketplace/prices/latest?{q}").status_code == 422


def test_unknown_route_gives_404(stub):
    assert stub.raw("GET", "/nothing").status_code == 404
    assert (
        stub.raw(
            "GET", "/da/nothing", headers={"Authorization": f"Bearer {stub.token()}"}
        ).status_code
        == 404
    )
