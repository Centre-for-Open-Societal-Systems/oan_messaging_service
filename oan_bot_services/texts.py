"""Renders a text key from ``text-keys.yaml`` (English and Amharic) for the reduced
runtime, where Bot Services itself sends every reply through OAN Messaging.

Only catalogued keys and their declared params are rendered; anything else is a bug and
raises. A param whose value is itself a catalogued key (``staff.linked`` takes
``role_name = role.supervisor``) is rendered in the same language. Buttons are checked
against OAN Messaging's reply keyboard rule (1-30 UTF-16 units, no control characters).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from oan_bot_services import text_keys as tk

LANGUAGES = ("en", "am")
DEFAULT_LANGUAGE = "en"
MAX_BUTTON_UTF16 = 30
MAX_TEXT_UTF16 = 4096


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _bad_chars(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


class Catalogue:
    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        self.entries = entries
        missing = sorted(tk.ALL - set(entries))
        if missing:
            raise ValueError(f"text-keys.yaml lacks keys: {', '.join(missing)}")
        for key in tk.ALL:
            entry = entries[key]
            for lang in LANGUAGES:
                if not isinstance(entry.get(lang), str) or not entry[lang]:
                    raise ValueError(f"text-keys.yaml: {key} has no {lang} text")
            if key in tk.BUTTONS:
                for lang in LANGUAGES:
                    label = entry[lang]
                    if _utf16(label) > MAX_BUTTON_UTF16 or _bad_chars(label):
                        raise ValueError(f"text-keys.yaml: button {key} ({lang}) is not valid")

    def render(self, key: str, language: str | None, params: dict[str, Any] | None = None) -> str:
        if key not in tk.ALL:
            raise ValueError("unknown text key")
        lang = language if language in LANGUAGES else DEFAULT_LANGUAGE
        entry = self.entries[key]
        declared = tuple(entry.get("params") or ())
        given = dict(params or {})
        if set(given) != set(declared):
            raise ValueError(f"params for {key} do not match the catalogue")
        values = {
            name: self.render(v, lang) if isinstance(v, str) and v in tk.ALL else v
            for name, v in given.items()
        }
        text = str(entry[lang]).format_map(values)
        if _utf16(text) > MAX_TEXT_UTF16:
            raise ValueError(f"{key} renders longer than Telegram allows")
        return text

    def labels(self, key: str) -> set[str]:
        """Every language's label of a button, for matching what the farmer tapped."""
        return {str(self.entries[key][lang]) for lang in LANGUAGES}


@cache
def load(path: str) -> Catalogue:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = {k: v for group in raw.values() for k, v in group.items()}
    return Catalogue(entries)
