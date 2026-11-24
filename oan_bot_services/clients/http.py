"""Shared HTTP behaviour for every adapter (§5.7).

* Timeout 5 seconds (configurable).
* One retry, only when the connection could not be made (the request never left).
* Circuit breaker: 5 failures within 60 seconds open it; while open, calls fail at once
  with ``Unavailable``; after 60 seconds one trial call is let through.
* Errors never carry URLs, bodies or tokens: ``Unavailable`` has a fixed message.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from oan_bot_services import logs

log = logs.get(__name__)

FAILURES_TO_OPEN = 5
WINDOW_SECONDS = 60.0
OPEN_SECONDS = 60.0


class Unavailable(Exception):
    """The service did not answer usefully (down, timeout, 5xx, breaker open)."""

    def __init__(self, service: str, reason: str) -> None:
        super().__init__(f"{service} unavailable: {reason}")
        self.service, self.reason = service, reason


class Breaker:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._failures: deque[float] = deque()
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return self._now() < self._open_until

    def success(self) -> None:
        self._failures.clear()
        self._open_until = 0.0

    def failure(self) -> None:
        now = self._now()
        self._failures.append(now)
        while self._failures and self._failures[0] <= now - WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) >= FAILURES_TO_OPEN:
            self._open_until = now + OPEN_SECONDS
            self._failures.clear()


TokenSource = Callable[[], Awaitable[str | None]]


class ServiceClient:
    """One upstream service. ``auth`` gives the bearer token (or None for no header)."""

    def __init__(
        self,
        service: str,
        base_url: str,
        client: httpx.AsyncClient,
        *,
        auth: TokenSource | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
        breaker: Breaker | None = None,
    ) -> None:
        self.service = service
        self.base = base_url.rstrip("/")
        self._client = client
        self._auth = auth
        self._headers = dict(headers or {})
        self._timeout = timeout
        self.breaker = breaker or Breaker()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Returns the response when its status is in ``ok`` or is a 4xx the caller
        handles (raised as ``Unavailable`` otherwise). 5xx, timeouts and connection
        errors count against the breaker."""
        if not self.base:
            raise Unavailable(self.service, "not_configured")
        if self.breaker.is_open:
            raise Unavailable(self.service, "circuit_open")
        all_headers = dict(self._headers)
        if self._auth is not None:
            token = await self._auth()
            if token:
                all_headers["Authorization"] = f"Bearer {token}"
        all_headers.update(headers or {})
        url = self.base + path
        for attempt in (1, 2):
            try:
                response = await self._client.request(
                    method, url, json=json, params=params, data=data, headers=all_headers,
                    timeout=self._timeout, follow_redirects=False,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                if attempt == 1:
                    continue  # never left: one retry (§5.7)
                self._fail("connect")
            except httpx.TimeoutException:
                self._fail("timeout")
            except httpx.HTTPError:
                self._fail("transport")
            else:
                if response.status_code >= 500:
                    self._fail(f"http_{response.status_code}")
                self.breaker.success()
                if response.status_code in ok or 400 <= response.status_code < 500:
                    return response
                raise Unavailable(self.service, f"http_{response.status_code}")
        raise AssertionError("unreachable")  # pragma: no cover

    def _fail(self, reason: str) -> None:
        self.breaker.failure()
        log.warning("upstream failure service=%s reason=%s", self.service, reason)
        raise Unavailable(self.service, reason)


def json_object(response: httpx.Response, service: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise Unavailable(service, "malformed") from None
    if not isinstance(body, dict):
        raise Unavailable(service, "malformed")
    return body
