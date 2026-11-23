"""Settings from the environment (prefix ``OAN_BOT_``), validated at start-up.

Secrets are ``SecretStr`` and never logged. ``OAN_BOT_CLOCK_URL`` (the fake clock) is
refused unless ``OAN_BOT_ENV=local``.

    OAN_BOT_ENV=local|dev|staging|production
    OAN_BOT_DATABASE_URL=postgresql+psycopg://...
    OAN_BOT_FLOW_SECRET=<shared secret, RapidPro global>
    OAN_BOT_CONTACT_REF_KEY=<base64, >= 32 bytes>      HMAC key of contact_ref
    OAN_BOT_FINGERPRINT_KEY=<base64, >= 32 bytes>      idempotency and phone fingerprints
    OAN_BOT_DATA_KEY=<base64, 32 bytes>                AES-GCM: linked phone, broadcast text
    OAN_BOT_BROADCAST_TOKEN_SHA256=<hex>[,<hex>]       accepted DA backend bearer tokens
    OAN_BOT_CLIENT_ID / OAN_BOT_CLIENT_SECRET / OAN_BOT_TOKEN_URL   client credentials
    OAN_BOT_DA_URL, OAN_BOT_AUTH_URL, OAN_BOT_BENEFICIARY_URL,
    OAN_BOT_MASTER_DATA_URL, OAN_BOT_MARKETPLACE_URL
    OAN_BOT_RAPIDPRO_URL, OAN_BOT_RAPIDPRO_TOKEN, OAN_BOT_RAPIDPRO_NOTIFY_FLOW
    OAN_BOT_OAN_MSG_URL, OAN_BOT_OAN_MSG_TOKEN
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "dev", "staging", "production"]


def decode_key(value: SecretStr, *, name: str, exact: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(value.get_secret_value(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{name} is not valid base64") from None
    if exact is not None and len(raw) != exact:
        raise ValueError(f"{name} must be {exact} bytes")
    if len(raw) < 32:
        raise ValueError(f"{name} must be at least 32 bytes")
    return raw


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAN_BOT_", extra="ignore", frozen=True)

    env: Env = "production"
    database_url: SecretStr
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=5, ge=0)

    flow_secret: SecretStr
    contact_ref_key: SecretStr
    fingerprint_key: SecretStr
    data_key: SecretStr
    #: SHA-256 (hex) of each bearer token the DA backend may use on /broadcasts. Phase A
    #: stand-in for Keycloak token validation (the stub has no introspection).
    broadcast_token_sha256: str = ""

    clock_url: str | None = None

    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    token_url: str = ""
    da_url: str = ""
    auth_url: str = ""
    beneficiary_url: str = ""
    master_data_url: str = ""
    marketplace_url: str = ""

    rapidpro_url: str = ""
    rapidpro_token: SecretStr = SecretStr("")
    rapidpro_notify_flow: str = ""
    rapidpro_staff_group: str = "staff"

    oan_msg_url: str = ""
    oan_msg_token: SecretStr = SecretStr("")

    #: Seconds; §5.7.
    client_timeout: float = Field(default=5.0, gt=0, le=30)
    #: Master Data cache (§5.6).
    area_cache_seconds: int = Field(default=24 * 3600, gt=0)
    #: TG-33 / §6.8.1: subscription removed after this many days without activity.
    subscription_inactive_days: int = Field(default=365, gt=0)
    #: How long a flow call's answer is kept for a repeat with the same request_id.
    replay_days: int = Field(default=7, gt=0)
    #: Broadcast idempotency bindings (§6.8.2: at least 30 days).
    broadcast_binding_days: int = Field(default=30, ge=30)
    #: Polling OAN Messaging: at most once every this many seconds per broadcast.
    poll_interval_seconds: int = Field(default=10, ge=10)
    #: Recipients submitted or polled per worker cycle.
    batch_size: int = Field(default=200, gt=0, le=1000)
    #: Default broadcast expiry when the request has none (inside OAN's horizon).
    default_broadcast_hours: int = Field(default=24, gt=0, le=24 * 7)

    @field_validator("broadcast_token_sha256")
    @classmethod
    def _hex_list(cls, value: str) -> str:
        for item in filter(None, (v.strip() for v in value.split(","))):
            if len(item) != 64 or any(c not in "0123456789abcdef" for c in item.lower()):
                raise ValueError("broadcast_token_sha256 must be hex SHA-256 digests")
        return value

    @model_validator(mode="after")
    def _clock_only_local(self) -> BotSettings:
        if self.clock_url and self.env != "local":
            raise ValueError("OAN_BOT_CLOCK_URL is allowed only with OAN_BOT_ENV=local")
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("database URL must use the postgresql+psycopg driver")
        for name in ("contact_ref_key", "fingerprint_key"):
            decode_key(getattr(self, name), name=name)
        decode_key(self.data_key, name="data_key", exact=32)
        if len(self.flow_secret.get_secret_value()) < 16:
            raise ValueError("flow_secret must be at least 16 characters")
        return self

    def broadcast_digests(self) -> tuple[bytes, ...]:
        return tuple(
            bytes.fromhex(v.strip())
            for v in self.broadcast_token_sha256.split(",")
            if v.strip()
        )

    def secrets(self) -> tuple[str, ...]:
        """Every secret value, for the log redaction filter."""
        values = (
            self.database_url, self.flow_secret, self.contact_ref_key, self.fingerprint_key,
            self.data_key, self.client_secret, self.rapidpro_token, self.oan_msg_token,
        )
        return tuple(v.get_secret_value() for v in values if v.get_secret_value())


def same_secret(given: str | None, expected: SecretStr) -> bool:
    """Constant-time comparison (§5.8). An empty expected value never matches."""
    want = expected.get_secret_value().encode()
    if not want or given is None:
        return False
    return hmac.compare_digest(given.encode(), want)
