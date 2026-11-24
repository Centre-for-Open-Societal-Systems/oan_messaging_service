"""Schema and content checks of seed.json: contract shapes, made-up data only, and the
cases the brief asks for."""

from __future__ import annotations

import json
import re
from collections import Counter

from stubs_contract import LOADED, STUBS

SEED = json.loads((STUBS / "seed.json").read_text(encoding="utf-8"))
AREAS = {a["area_code"]: a for a in SEED["areas"]}
AREA_CODE = re.compile(r"^[A-Z0-9]{2,16}$")
TEST_PHONE = re.compile(r"^\+251900000[0-9]{3}$")


def test_areas_match_the_contract_and_shape():
    md = LOADED["master-data"]
    for a in SEED["areas"]:
        md.validate("/components/schemas/Area", a)
        assert AREA_CODE.match(a["area_code"])
        assert a["name_en"] and a["name_am"] and a["name_am"] != a["name_en"]
        assert re.search("[ሀ-፿]", a["name_am"]), "Amharic name in Ethiopic script"
    levels = Counter(a["level"] for a in SEED["areas"])
    assert levels == {"region": 2, "zone": 4, "woreda": 8, "kebele": 24}
    for a in SEED["areas"]:
        if a["level"] != "region":
            parent = AREAS[a["parent_code"]]
            assert ("region", "zone", "woreda", "kebele").index(parent["level"]) + 1 == (
                "region",
                "zone",
                "woreda",
                "kebele",
            ).index(a["level"])
            assert a["area_code"].startswith(parent["area_code"])
    # made-up codes: the regions ET91 and ET92 do not exist
    assert {a["area_code"] for a in SEED["areas"] if a["level"] == "region"} == {"ET91", "ET92"}


def test_farmers_match_the_beneficiary_contract():
    ben = LOADED["beneficiary"]
    farmers = SEED["farmers"]
    assert 25 <= len(farmers) <= 35
    assert len({f["farmer_id"] for f in farmers}) == len(farmers)
    for f in farmers:
        ben.validate("/components/schemas/Farmer", f)
        assert f["farmer_id"].startswith("FR-TEST-")
        for p in f["phones"]:
            assert TEST_PHONE.match(p["number"]), "test numbers +251900000xxx only"
        assert f["primary_phone"] == next(
            (p["number"] for p in f["phones"] if p["is_primary"]), None
        )
        assert f["first_name"].startswith("Testfarmer")
        for level in ("region", "zone", "woreda"):
            assert f[f"{level}_code"] in AREAS
    assert sum(1 for f in farmers if not f["phones"]) >= 2, "some farmers have no phone"
    unknown = [f for f in farmers if f["kebele_code"] not in AREAS]
    assert len(unknown) == 1, "exactly one farmer has an unknown kebele"
    kebeles = {f["kebele_code"] for f in farmers}
    assert len(kebeles) >= 10, "farmers are spread over the kebeles"


def test_people():
    people = SEED["people"]
    refs = [p["ref"] for p in people]
    assert len(set(refs)) == len(refs)
    agents = [p for p in people if p["role"] == "development_agent"]
    assert 8 <= len(agents) <= 12
    da = LOADED["da"]
    for a in agents:
        assert a["kebele_code"] in AREAS and AREAS[a["kebele_code"]]["level"] == "kebele"
        da.validate(
            "/components/schemas/Person",
            {
                "ref": a["ref"],
                "role": a["role"],
                "display_name": a["display_name"],
                "specialisation": a["specialisation"],
                "telegram_linked": a["telegram_id"] is not None,
            },
        )
    pairs = Counter(
        (a["kebele_code"], a["specialisation"])
        for a in agents
        if a["posting_effective"] and a["grant_active"]
    )
    assert max(pairs.values()) >= 2, "two agents share a specialisation in one kebele"
    assert any(a["telegram_id"] is None for a in agents), "some agents are not linked"
    sup = Counter(p["scope_code"] for p in people if p["role"] == "supervisor")
    woredas = sorted(c for c, a in AREAS.items() if a["level"] == "woreda")
    assert sorted(sup.values()) == [1, 2]
    assert any(w not in sup for w in woredas), "a woreda with no Supervisor"
    for p in people:
        assert p["display_name"].startswith("Test ")
        if p.get("telegram_id") is not None:
            assert isinstance(p["telegram_id"], int)
        if p["role"] != "development_agent":
            assert p["scope_code"] in AREAS and AREAS[p["scope_code"]]["level"] == p["scope_level"]


def test_link_codes():
    refs = {p["ref"] for p in SEED["people"]}
    for c in SEED["link_codes"]:
        assert re.match(r"^[A-Za-z0-9]{8}$", c["code"]) and c["ref"] in refs
    roles = {
        next(p["role"] for p in SEED["people"] if p["ref"] == c["ref"]) for c in SEED["link_codes"]
    }
    assert roles == {"development_agent", "supervisor", "executive"}


def test_routing_modes():
    for code, mode in SEED["kebele_routing_modes"].items():
        assert code in AREAS and mode in ("specialism", "kebele_inbox")


def test_market_data():
    assert len(SEED["commodities"]) == 10
    mk = SEED["markets"]
    assert Counter(m["woreda_code"] for m in mk) == {
        w: 2 for w, a in AREAS.items() if a["level"] == "woreda"
    }
    stale = [m for m in mk if m["price_age_days"] > 7]
    assert len(stale) == 1, "one market with an old price"
    ids = {m["market_id"] for m in mk}
    cids = {c["commodity_id"] for c in SEED["commodities"]}
    for miss in SEED["missing_prices"]:
        assert miss["market_id"] in ids and miss["commodity_id"] in cids


def test_no_real_hosts_or_numbers():
    text = (STUBS / "seed.json").read_text(encoding="utf-8")
    assert not re.search(r"https?://", text)
    assert not re.search(r"@", text)
    for num in re.findall(r"\+?251\d+", text):
        assert TEST_PHONE.match(num), num
