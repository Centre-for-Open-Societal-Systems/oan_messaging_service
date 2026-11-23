"""Contract tests: routes match ``bot-services.openapi.yaml`` and the DA kit's broadcast
OpenAPI; Bot Services can emit no text key missing from ``text-keys.yaml``."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from contract import CONTRACTS, spec

from oan_bot_services import text_keys as tk
from oan_bot_services.api import models as m
from oan_bot_services.api.app import create_app

PACKAGE = Path(__file__).resolve().parents[2] / "oan_bot_services"


def catalogue() -> dict[str, dict]:
    raw = yaml.safe_load((CONTRACTS / "text-keys.yaml").read_text())
    return {k: v for group in raw.values() for k, v in group.items()}


def app_routes() -> set[tuple[str, str]]:
    app = create_app(deps=None)
    out = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            if method != "HEAD":
                out.add((method, route.path))  # type: ignore[attr-defined]
    return out


def test_every_contract_route_exists_and_nothing_else() -> None:
    expected = set()
    for name in ("bot", "broadcasts"):
        for path, item in spec(name)["paths"].items():
            for method in item:
                expected.add((method.upper(), path))
    assert app_routes() == expected


def test_request_models_match_the_contract_fields() -> None:
    doc = spec("bot")
    pairs = {
        ("/contacts/location", "post"): m.LocationIn,
        ("/contacts/updates", "put"): m.UpdatesIn,
        ("/contacts/idle", "post"): m.IdleIn,
        ("/contacts/stop", "post"): m.StopIn,
        ("/staff/link", "post"): m.StaffLinkIn,
        ("/staff/inbound", "post"): m.StaffInboundIn,
        ("/farmer-link/start", "post"): m.FarmerLinkStartIn,
        ("/farmer-link/verify", "post"): m.FarmerLinkVerifyIn,
        ("/farmer-link", "delete"): m.UnlinkIn,
    }
    for (path, method), model in pairs.items():
        schema = doc["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
        assert set(schema["properties"]) == set(model.model_fields), path
        required = {n for n, f in model.model_fields.items() if f.is_required()}
        assert set(schema["required"]) == required, path
        assert schema["additionalProperties"] is False
    bc = spec("broadcasts")["paths"]["/broadcasts"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert set(bc["properties"]) == set(m.BroadcastIn.model_fields)
    assert set(bc["required"]) == {
        n for n, f in m.BroadcastIn.model_fields.items() if f.is_required()
    }


def test_every_emitted_key_is_in_the_catalogue_with_its_params() -> None:
    cat = catalogue()
    assert tk.ALL <= set(cat), sorted(tk.ALL - set(cat))
    for key, params in tk.PARAMS.items():
        assert tuple(cat[key].get("params", ())) == params, key
    for key in tk.ALL:
        if key not in tk.PARAMS and not key.startswith("role."):
            assert not cat[key].get("params"), key


def test_no_key_literal_outside_the_constants() -> None:
    """Code refers to keys only through ``text_keys``: no other module may hold a string
    that looks like a key, so nothing can slip past the catalogue check."""
    prefixes = "|".join(sorted({k.split(".")[0] for k in catalogue()}))
    pattern = re.compile(rf"""["']((?:{prefixes})\.[a-z_]+)["']""")
    for path in PACKAGE.rglob("*.py"):
        if path.name == "text_keys.py":
            continue
        found = pattern.findall(path.read_text())
        assert not found, (path.name, found)


def test_error_text_keys_are_catalogued() -> None:
    from oan_bot_services.errors import invalid_choice, unavailable

    cat = catalogue()
    assert invalid_choice().text_key in cat and unavailable().text_key in cat
