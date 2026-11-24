"""Everything a route or job needs, built once per process from ``BotSettings``."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from oan_bot_services import logs, texts
from oan_bot_services.clients.http import ServiceClient
from oan_bot_services.clients.oan_messaging import OanMessaging
from oan_bot_services.clients.rapidpro import RapidPro
from oan_bot_services.clients.services import (
    AuthOtp,
    Beneficiary,
    DaBackend,
    Marketplace,
    MasterData,
)
from oan_bot_services.clients.tokens import ClientCredentials
from oan_bot_services.clock import Clock, make_clock
from oan_bot_services.crypto import Keys
from oan_bot_services.db import make_engine, make_session_factory
from oan_bot_services.settings import BotSettings, decode_key


@dataclass
class Deps:
    settings: BotSettings
    keys: Keys
    engine: AsyncEngine
    sf: async_sessionmaker[AsyncSession]
    clock: Clock
    http: httpx.AsyncClient
    da: DaBackend
    auth: AuthOtp
    beneficiary: Beneficiary
    master_data: MasterData
    marketplace: Marketplace
    #: None when ``OAN_BOT_RAPIDPRO_ENABLED`` is false: nothing calls RapidPro.
    rapidpro: RapidPro | None
    oan: OanMessaging

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.engine.dispose()


def make_http() -> httpx.AsyncClient:
    """Certificates verified, no redirects, no proxies from the environment."""
    return httpx.AsyncClient(verify=True, follow_redirects=False, trust_env=False)


def build(settings: BotSettings, *, http: httpx.AsyncClient | None = None) -> Deps:
    logs.install(*settings.secrets())
    keys = Keys(
        decode_key(settings.contact_ref_key, name="contact_ref_key"),
        decode_key(settings.fingerprint_key, name="fingerprint_key"),
        decode_key(settings.data_key, name="data_key", exact=32),
    )
    engine = make_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    client = http or make_http()
    t = settings.client_timeout
    tokens = ClientCredentials(
        settings.token_url, settings.client_id, settings.client_secret, client, timeout=t
    )

    def svc(name: str, url: str) -> ServiceClient:
        return ServiceClient(name, url, client, auth=tokens, timeout=t)

    rapidpro: RapidPro | None = None
    if not settings.rapidpro_enabled:
        texts.load(settings.text_keys_file)  # every key, both languages: fail at start-up
    if settings.rapidpro_enabled:
        rapidpro_token = settings.rapidpro_token.get_secret_value()
        rapidpro = RapidPro(
            ServiceClient(
                "rapidpro", settings.rapidpro_url, client, timeout=t,
                headers={"Authorization": f"Token {rapidpro_token}"} if rapidpro_token else None,
            ),
            settings.rapidpro_notify_flow,
            settings.rapidpro_staff_group,
        )
    oan_token = settings.oan_msg_token.get_secret_value()

    async def oan_auth() -> str | None:
        return oan_token or None

    return Deps(
        settings=settings,
        keys=keys,
        engine=engine,
        sf=make_session_factory(engine),
        clock=make_clock(settings.clock_url, client),
        http=client,
        da=DaBackend(svc("da", settings.da_url)),
        auth=AuthOtp(svc("auth", settings.auth_url)),
        beneficiary=Beneficiary(svc("beneficiary", settings.beneficiary_url)),
        master_data=MasterData(svc("master_data", settings.master_data_url)),
        marketplace=Marketplace(svc("marketplace", settings.marketplace_url)),
        rapidpro=rapidpro,
        oan=OanMessaging(ServiceClient("oan_messaging", settings.oan_msg_url, client,
                                       auth=oan_auth, timeout=t)),
    )
