"""Client-credentials token for the OAN services (§5.7; Keycloak, or ``stubs/auth`` in the
local stack). Cached until 30 seconds before it expires. Never logged."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr

from oan_bot_services.clients.http import Unavailable

SKEW_SECONDS = 30


class ClientCredentials:
    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        client: httpx.AsyncClient,
        *,
        timeout: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url, self._id, self._secret = token_url, client_id, client_secret
        self._client, self._timeout, self._now = client, timeout, monotonic
        self._token: str | None = None
        self._until = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "ClientCredentials(<redacted>)"

    async def __call__(self) -> str | None:
        if not self._url:
            return None
        async with self._lock:
            if self._token and self._now() < self._until:
                return self._token
            try:
                r = await self._client.post(
                    self._url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._id,
                        "client_secret": self._secret.get_secret_value(),
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                raise Unavailable("token", "transport") from None
            if r.status_code != 200:
                raise Unavailable("token", f"http_{r.status_code}")
            body = r.json()
            token = body.get("access_token") if isinstance(body, dict) else None
            expires = body.get("expires_in", 60) if isinstance(body, dict) else 60
            if not isinstance(token, str) or not token:
                raise Unavailable("token", "malformed")
            self._token = token
            self._until = self._now() + max(int(expires) - SKEW_SECONDS, 1)
            return token

    def forget(self) -> None:
        """After a 401 from a service: fetch a new token next time."""
        self._token, self._until = None, 0.0
