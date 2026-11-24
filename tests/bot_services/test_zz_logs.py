"""Runs last in this folder: no log line written during the Bot Services tests holds a
chat id, Farmer ID, phone, code, token or message text the tests used (§5.4), in either
runtime (the webhook tests register the texts they send)."""

from __future__ import annotations

from botenv import LOG_LINES
from botsupport import SENSITIVE


def test_no_personal_data_or_secret_in_any_log_line() -> None:
    assert LOG_LINES, "no log lines captured: the check would prove nothing"
    values = {v for v in SENSITIVE if len(v) >= 4}
    leaks = [(v, line) for line in LOG_LINES for v in values if v in line]
    assert not leaks, leaks[:5]
    assert any("contact_ref=" in line for line in LOG_LINES)
    # The reduced runtime (webhook, conversation, cancels) was exercised and logged too.
    for marker in ("update stored contact_ref=", "farmer linked contact_ref=",
                   "stop contact_ref=", "linking locked scope="):
        assert any(marker in line for line in LOG_LINES), marker
