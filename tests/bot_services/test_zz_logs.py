"""Runs last in this folder: no log line written during the Bot Services tests holds a
chat id, Farmer ID, phone, code or token the tests used (§5.4)."""

from __future__ import annotations

from botsupport import SENSITIVE
from conftest import LOG_LINES


def test_no_personal_data_or_secret_in_any_log_line() -> None:
    assert LOG_LINES, "no log lines captured: the check would prove nothing"
    values = {v for v in SENSITIVE if len(v) >= 4}
    leaks = [(v, line) for line in LOG_LINES for v in values if v in line]
    assert not leaks, leaks[:5]
    assert any("contact_ref=" in line for line in LOG_LINES)
