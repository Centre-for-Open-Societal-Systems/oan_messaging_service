"""Errors a flow route answers with: ``{error, text_key}`` (§5.8)."""

from __future__ import annotations

from oan_bot_services import text_keys as tk


class FlowError(Exception):
    def __init__(self, status: int, error: str, text_key: str) -> None:
        super().__init__(error)
        self.status, self.error, self.text_key = status, error, text_key

    def body(self) -> dict[str, str]:
        return {"error": self.error, "text_key": self.text_key}


def unavailable() -> FlowError:
    return FlowError(503, "unavailable", tk.COMMON_UNAVAILABLE)


def invalid_choice() -> FlowError:
    return FlowError(422, "validation_failed", tk.COMMON_INVALID_CHOICE)
