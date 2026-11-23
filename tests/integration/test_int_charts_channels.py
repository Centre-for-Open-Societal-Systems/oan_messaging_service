"""Contract 3.3.0: charts count every channel and label every row with its channel.

The Telegram copy of a DA broadcast (``bc:<broadcast_id>:<n>``, sent by OAN Bot Services)
and its SMS copy (``da_broadcast:<broadcast_id>:<who>``) are two labelled campaign rows."""

from __future__ import annotations

from typing import Any

import httpx
from itsupport import Harness, fake_number, hdr, new_key

from oan_messaging.contracts import Channel
from oan_messaging.stats.snapshot import refresh

COUNTED = ("msgByCaller", "msgByPurpose", "msgFailureReasons", "msgOtpLatency", "msgCampaigns")


async def charts(h: Harness, token: str, path: str, **params: Any) -> httpx.Response:
    return await h.api.get(f"/api/v1/charts{path}", params=params, headers=hdr(token))


async def data(h: Harness, token: str, chart: str, **params: Any) -> list[dict[str, Any]]:
    r = await charts(h, token, f"/{chart}", **params)
    assert r.status_code == 200, r.text
    return list(r.json()["data"])


async def seed_both_channels(h: Harness) -> str:
    """One DA broadcast BC-9: 3 SMS copies and 2 Telegram copies (one blocked); plus one
    OTP by SMS. Returns a statistics reader token."""
    da = await h.caller("da", bot=None, channels=frozenset({Channel.SMS}))
    bots = await h.caller("bot-services", bot="grievance", channels=frozenset({Channel.TELEGRAM}))
    for n in (701, 702, 703):
        r = await h.post_sms(
            da, new_key(), recipient=fake_number(n), purpose="broadcast", expires_at=None,
            text="Rain expected", source_reference=f"da_broadcast:BC-9:FR-{n}",
        )
        assert r.status_code == 202, r.text
    blocked_chat = 555000902

    def telegram(body: dict[str, Any], n: int) -> httpx.Response:
        chat_id = body["chat_id"]
        if chat_id == blocked_chat:
            return httpx.Response(
                403, json={"ok": False, "error_code": 403,
                           "description": "Forbidden: bot was blocked by the user"},
            )
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 900 + n, "chat": {"id": chat_id}}}
        )

    h.tg.script = telegram
    for i, chat in enumerate((555000901, blocked_chat), start=1):
        r = await h.post_tg(
            bots, new_key(), chat_id=chat, purpose="broadcast", text="Rain expected",
            source_reference=f"bc:BC-9:{i}",
        )
        assert r.status_code == 202, r.text
    esignet = await h.caller("esignet", bot=None)
    assert (await h.post_sms(esignet, new_key(), recipient=fake_number(704))).status_code == 202
    await h.worker(batch_size=10).run_once()
    assert await refresh(h.sf, h.clock())
    return await h.caller("reports-central", stats_reader=True)


async def test_kpis_and_trend_keep_the_overall_row_and_add_each_channel(h: Harness) -> None:
    token = await seed_both_channels(h)
    [k] = await data(h, token, "msgKpis")
    assert k["channel"] == "all" and k["accepted"] == 6 and k["failed"] == 1
    per = {r["channel"]: r for r in k["by_channel"]}
    assert [r["channel"] for r in k["by_channel"]] == ["sms", "telegram"]
    assert (per["sms"]["accepted"], per["telegram"]["accepted"]) == (4, 2)
    assert (per["telegram"]["sent"], per["telegram"]["failed"]) == (1, 1)
    assert sum(r["accepted"] for r in k["by_channel"]) == k["accepted"]
    assert set(per["sms"]) == set(k) - {"channel", "by_channel"} | {"channel"}

    [t] = await data(h, token, "msgTrend", granularity="month")
    assert t["channel"] == "all" and t["accepted"] == 6
    assert [(r["channel"], r["accepted"]) for r in t["by_channel"]] == [("sms", 4), ("telegram", 2)]

    # The channel filter still limits everything, the overall row included.
    [only_tg] = await data(h, token, "msgKpis", channel="telegram")
    assert only_tg["accepted"] == 2
    assert [r["channel"] for r in only_tg["by_channel"]] == ["telegram"]


async def test_every_counting_row_says_its_channel(h: Harness) -> None:
    token = await seed_both_channels(h)
    for chart in COUNTED:
        rows = await data(h, token, chart)
        assert rows, chart
        assert all(r["channel"] in ("sms", "telegram") for r in rows), chart

    by_caller = {(r["caller"], r["channel"]): r["accepted"]
                 for r in await data(h, token, "msgByCaller")}
    assert by_caller == {("da", "sms"): 3, ("bot-services", "telegram"): 2, ("esignet", "sms"): 1}
    by_purpose = [(r["purpose"], r["channel"], r["accepted"])
                  for r in await data(h, token, "msgByPurpose")]
    assert ("broadcast", "sms", 3) in by_purpose and ("broadcast", "telegram", 2) in by_purpose
    reasons = await data(h, token, "msgFailureReasons")
    assert [(r["failure_code"], r["channel"], r["share"]) for r in reasons] == [
        ("recipient_blocked_bot", "telegram", 1.0)
    ]
    [otp] = await data(h, token, "msgOtpLatency")
    assert otp["channel"] == "sms" and otp["accepted"] == 1


async def test_both_copies_of_a_broadcast_are_two_labelled_campaign_rows(h: Harness) -> None:
    token = await seed_both_channels(h)
    rows = await data(h, token, "msgCampaigns")
    got = sorted((r["campaign"], r["channel"], r["recipients"], r["sent"], r["failed"])
                 for r in rows)
    assert got == [("bc:BC-9", "telegram", 2, 1, 1), ("da_broadcast:BC-9", "sms", 3, 3, 0)]
    sms_only = await data(h, token, "msgCampaigns", channel="sms")
    assert [r["campaign"] for r in sms_only] == ["da_broadcast:BC-9"]


async def test_sms_only_data_gives_the_same_numbers_as_before(h: Harness) -> None:
    """With SMS only, every chart has the rows and numbers of 3.2.0 plus the channel."""
    da = await h.caller("da", bot=None)
    for n in (801, 802):
        r = await h.post_sms(da, new_key(), recipient=fake_number(n), purpose="broadcast",
                             expires_at=None, text="x", source_reference=f"da_broadcast:B:{n}")
        assert r.status_code == 202
    await h.worker(batch_size=10).run_once()
    assert await refresh(h.sf, h.clock())
    token = await h.caller("reports-central", stats_reader=True)
    [k] = await data(h, token, "msgKpis")
    # A stable shape: both channels, zeros for the one with no traffic.
    assert k["accepted"] == 2 and [r["channel"] for r in k["by_channel"]] == ["sms", "telegram"]
    assert [r["accepted"] for r in k["by_channel"]] == [2, 0]
    assert k["by_channel"][1]["delivered_share"] is None
    assert [(r["caller"], r["channel"]) for r in await data(h, token, "msgByCaller")] == [
        ("da", "sms")
    ]
