"""Entry points.

    python -m oan_bot_services api [--host 0.0.0.0] [--port 8100]
    python -m oan_bot_services worker
    python -m oan_bot_services migrate
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from oan_bot_services import logs


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from oan_bot_services.settings import BotSettings

    settings = BotSettings()
    logs.install(*settings.secrets())
    cfg = Config(str(Path(__file__).parent / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = settings.database_url.get_secret_value()
    command.upgrade(cfg, "head")


async def _worker() -> None:
    from oan_bot_services import worker
    from oan_bot_services.deps import build
    from oan_bot_services.settings import BotSettings

    deps = build(BotSettings())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(deps, stop)
    finally:
        await deps.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oan_bot_services")
    parser.add_argument("component", choices=["api", "worker", "migrate"])
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 (container ingress)
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logs.install()
    if args.component == "migrate":
        _migrate()
    elif args.component == "worker":
        asyncio.run(_worker())
    else:
        import uvicorn

        uvicorn.run("oan_bot_services.api.app:create_app", factory=True, host=args.host,
                    port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
