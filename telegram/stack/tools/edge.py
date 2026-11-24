"""Edge forwarder: the only container on a network the host can reach.

Docker cannot publish ports from an `internal` network, so this container joins both the
internal stack network and a small `edge` network, and forwards a fixed list of TCP ports
to fixed internal services. It opens no other connection. Its own outbound traffic is
limited by iptables (see edge-entry.sh) to the internal subnet.

EDGE_ROUTES="28700=rapidpro-web:8000,28301=fake-telegram:8301"
"""

from __future__ import annotations

import asyncio
import os


def routes() -> list[tuple[int, str, int]]:
    out = []
    for item in os.environ["EDGE_ROUTES"].split(","):
        listen, target = item.strip().split("=")
        host, port = target.rsplit(":", 1)
        out.append((int(listen), host, int(port)))
    return out


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def handler(host: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except (TimeoutError, OSError):
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    return handle


async def main() -> None:
    servers = []
    for listen, host, port in routes():
        servers.append(await asyncio.start_server(handler(host, port), "0.0.0.0", listen))  # noqa: S104
        print(f"edge {listen} -> {host}:{port}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
