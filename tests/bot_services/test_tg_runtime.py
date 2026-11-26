"""The RapidPro switch: the reduced runtime starts with no RapidPro setting at all,
mounts only its own routes and runs no RapidPro job; the RapidPro runtime keeps working."""

from __future__ import annotations

import base64
import secrets
from datetime import timedelta

import httpx
import pytest
from botenv import WEBHOOK_SECRET, Bot, Tg, farmers_broadcast, link_farmer
from botsupport import new_contact, rid
from pydantic import ValidationError

from oan_bot_services import broadcasts as bc
from oan_bot_services import worker
from oan_bot_services.api.app import create_app
from oan_bot_services.deps import build
from oan_bot_services.settings import BotSettings


def b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


REDUCED_ENV = {
    "OAN_BOT_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/bot",
    "OAN_BOT_TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "OAN_BOT_CONTACT_REF_KEY": b64(),
    "OAN_BOT_FINGERPRINT_KEY": b64(),
    "OAN_BOT_DATA_KEY": b64(),
}


@pytest.fixture
def reduced_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("OAN_BOT_"):
            monkeypatch.delenv(name)
    for name, value in REDUCED_ENV.items():
        monkeypatch.setenv(name, value)


async def test_starts_with_rapidpro_off_and_no_rapidpro_settings(reduced_env: None) -> None:
    settings = BotSettings()
    assert settings.rapidpro_enabled is False
    assert settings.flow_secret.get_secret_value() == ""
    assert settings.rapidpro_url == "" and settings.rapidpro_token.get_secret_value() == ""
    deps = build(settings)
    try:
        assert deps.rapidpro is None
    finally:
        await deps.aclose()
    app = create_app()  # the uvicorn factory: settings from the environment
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/telegram/webhook" in paths and "/broadcasts" in paths
    assert not paths & {"/contacts/stop", "/farmer-link/start", "/staff/link"}


def test_webhook_secret_is_required_without_rapidpro(reduced_env: None,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("", "short-secret", "has space in it 12345", "x" * 257):
        monkeypatch.setenv("OAN_BOT_TELEGRAM_WEBHOOK_SECRET", bad)
        with pytest.raises(ValidationError, match="telegram_webhook_secret"):
            BotSettings()
    # With RapidPro on, the flow secret is required and the webhook secret is not.
    monkeypatch.setenv("OAN_BOT_RAPIDPRO_ENABLED", "true")
    with pytest.raises(ValidationError, match="flow_secret"):
        BotSettings()
    monkeypatch.setenv("OAN_BOT_FLOW_SECRET", "f" * 32)
    assert BotSettings().rapidpro_enabled is True
    assert WEBHOOK_SECRET not in repr(BotSettings())


def test_defaults_of_the_reduced_runtime(reduced_env: None) -> None:
    s = BotSettings()
    assert (s.stopped_retention_days, s.update_id_days, s.conversation_minutes,
            s.reply_minutes) == (90, 30, 30, 30)
    assert s.text_keys_file.endswith("telegram/contracts/text-keys.yaml")
    assert WEBHOOK_SECRET in s.secrets()


async def test_flow_routes_are_not_mounted(tg: Tg) -> None:
    r = await tg.api.post("/contacts/stop", json={})
    assert r.status_code == 404
    r = await tg.api.get("/healthz")
    assert r.status_code == 200


async def test_no_rapidpro_job_runs(tg: Tg, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(deps: object) -> int:
        raise AssertionError("a RapidPro job ran")

    calls: list[str] = []

    async def unreachable(deps: object) -> int:
        calls.append("unreachable_retry")
        return 0

    monkeypatch.setattr(worker.keyed, "release_idle", boom)
    monkeypatch.setattr(worker.stop, "retry_pending", boom)
    monkeypatch.setattr(worker.jobs, "retry_rapidpro_sync", boom)
    monkeypatch.setattr(worker.broadcasts, "retry_unreachable", unreachable)
    await worker.timer_job(tg.deps)
    assert calls == ["unreachable_retry"]


async def test_rapidpro_runtime_has_no_webhook_and_no_conversation_job(bot: Bot) -> None:
    r = await bot.api.post("/telegram/webhook", json={"update_id": 1},
                           headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET})
    assert r.status_code == 404
    await worker.conversation_job(bot.deps)  # does nothing, calls nothing
    assert not any(h == "oan-messaging.example.invalid" for _, h, _ in bot.up.calls)


async def test_rapidpro_updates_off_and_stop_also_cancel_handed_over_copies(bot: Bot) -> None:
    """The cancel rule applies to the old runtime too."""
    a, b = new_contact(700004001), new_contact(700004002)
    await link_farmer(bot, a, "FR-TEST-4001", "+251900004001", "ET01010101")
    await link_farmer(bot, b, "FR-TEST-4002", "+251900004002", "ET01010101")
    await farmers_broadcast(bot, "kebele", "ET01010101")
    await bc.submit_pending(bot.deps)
    await bot.flow("PUT", "/contacts/updates", {"request_id": rid(), "contact": a,
                                                "updates": "off"})
    await bot.flow("POST", "/contacts/stop", {"request_id": rid(), "contact": b})
    assert await bc.cancel_pending(bot.deps) == 2
    rows = await bot.q("SELECT status, failure_code FROM broadcast_recipients")
    assert sorted(rows) == [("failed", "cancelled"), ("failed", "cancelled")]
    bot.clock.now += timedelta(seconds=10)
    await bc.poll_results(bot.deps)
    assert await bot.q("SELECT state FROM broadcasts") == [("finished",)]


async def test_cancel_client_maps_every_answer() -> None:
    from oan_bot_services.clients.http import ServiceClient, Unavailable
    from oan_bot_services.clients.oan_messaging import OanMessaging

    answers = iter([200, 409, 404, 422, 401])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/cancel") and request.content == b""
        return httpx.Response(next(answers), json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    oan = OanMessaging(ServiceClient("oan_messaging", "http://oan.example.invalid", http))
    import uuid

    mid = uuid.uuid4()
    assert [await oan.cancel(mid) for _ in range(4)] == [
        "cancelled", "not_cancellable", "not_found", "not_cancellable"]
    with pytest.raises(Unavailable):
        await oan.cancel(mid)
    await http.aclose()


async def test_a_broken_text_keys_file_stops_the_start(reduced_env: None, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from oan_bot_services import texts

    bad = tmp_path / "text-keys.yaml"
    bad.write_text("common:\n  common.unavailable: {en: x, am: y}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lacks keys"):
        build(BotSettings(text_keys_file=str(bad)))
    texts.load.cache_clear()
    cat = texts.load(BotSettings().text_keys_file)
    with pytest.raises(ValueError, match="unknown text key"):
        cat.render("nothing.here", "en")
    with pytest.raises(ValueError, match="params"):
        cat.render("link.code_sent", "en", {})
    assert cat.render("menu.main", "xx") == cat.render("menu.main", "en")  # unknown language
