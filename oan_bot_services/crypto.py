"""``contact_ref``, keyed fingerprints and the data box (§2.4, §5.4).

* ``contact_ref`` = HMAC-SHA256(contact_ref key, chat id), hex. The only farmer identifier
  in logs, metrics and calls to OAN services.
* Fingerprints (idempotency keys, request bodies, phones) use a separate key.
* The data box (AES-GCM, random nonce) holds the few values that must be read back: the
  linked phone and a broadcast's text until it is submitted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Keys:
    def __init__(self, contact_ref_key: bytes, fingerprint_key: bytes, data_key: bytes) -> None:
        if len(data_key) != 32:
            raise ValueError("data key must be 32 bytes")
        self._ref = contact_ref_key
        self._fp = fingerprint_key
        self._box = AESGCM(data_key)

    def __repr__(self) -> str:
        return "Keys(<redacted>)"

    def contact_ref(self, telegram_id: int) -> str:
        return hmac.new(self._ref, str(int(telegram_id)).encode(), hashlib.sha256).hexdigest()

    def fingerprint(self, *parts: str) -> str:
        msg = "\x1f".join(parts).encode()
        return hmac.new(self._fp, msg, hashlib.sha256).hexdigest()

    def body_fingerprint(self, body: Any) -> str:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return self.fingerprint("body", canonical)

    def seal(self, plain: str, purpose: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._box.encrypt(nonce, plain.encode(), purpose.encode())

    def open(self, sealed: bytes, purpose: str) -> str:
        return self._box.decrypt(sealed[:12], sealed[12:], purpose.encode()).decode()


def masked_last_two(phone: str) -> str:
    digits = [c for c in phone if c.isdigit()]
    return "".join(digits[-2:])


def normalise_phone(phone: str) -> str | None:
    """E.164 Ethiopian mobile from what Telegram's contact button shares, else None."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("251") and len(digits) == 12:
        local = digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        local = digits[1:]
    elif len(digits) == 9:
        local = digits
    else:
        return None
    if local[0] not in "79":
        return None
    return "+251" + local
