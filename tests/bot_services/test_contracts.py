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
from oan_bot_services.settings import BotSettings
from oan_bot_services.texts import load

PACKAGE = Path(__file__).resolve().parents[2] / "oan_bot_services"


def catalogue() -> dict[str, dict]:
    raw = yaml.safe_load((CONTRACTS / "text-keys.yaml").read_text())
    return {k: v for group in raw.values() for k, v in group.items()}


def settings(rapidpro: bool) -> BotSettings:
    import base64
    import secrets

    def b64() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    return BotSettings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5432/bot", rapidpro_enabled=rapidpro,
        flow_secret="f" * 32 if rapidpro else "", telegram_webhook_secret="w" * 32,
        contact_ref_key=b64(), fingerprint_key=b64(), data_key=b64(),
    )


def app_routes(rapidpro: bool) -> set[tuple[str, str]]:
    app = create_app(deps=None, settings=settings(rapidpro))
    out = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            if method != "HEAD":
                out.add((method, route.path))  # type: ignore[attr-defined]
    return out


def test_every_contract_route_exists_and_nothing_else() -> None:
    """Each runtime mounts exactly the contract paths marked for it (``x-runtime``)."""
    for rapidpro, runtime in ((True, "rapidpro"), (False, "webhook")):
        expected = set()
        for name in ("bot", "broadcasts"):
            for path, item in spec(name)["paths"].items():
                if item.get("x-runtime", runtime) != runtime:
                    continue
                for method in item:
                    if not method.startswith("x-"):
                        expected.add((method.upper(), path))
        assert app_routes(rapidpro) == expected, runtime
    assert ("POST", "/telegram/webhook") not in app_routes(True)
    assert ("POST", "/contacts/stop") not in app_routes(False)


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


def test_the_catalogue_loads_for_the_reduced_runtime() -> None:
    """Every key renders in English and Amharic; buttons fit OAN Messaging's 30 units."""
    cat = load(str(CONTRACTS / "text-keys.yaml"))
    for key in tk.ALL:
        params = {p: "x" for p in cat.entries[key].get("params") or ()}
        for lang in ("en", "am"):
            assert cat.render(key, lang, params)
    for key in tk.BUTTONS:
        for label in cat.labels(key):
            assert len(label.encode("utf-16-le")) // 2 <= 30, key
