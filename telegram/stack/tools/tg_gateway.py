"""Local stand-in for the APISIX route in front of OAN Bot Services (spec §5.3).

One HTTPS listener (test-CA certificate) for `webhook.example.invalid` (also
`tg-gateway`). Only `POST /telegram/webhook` is allowed. The
`X-Telegram-Bot-Api-Secret-Token` header must equal the webhook secret (constant-time
compare); the body is at most 64 KB; then the request goes to OAN Bot Services, which
checks the header again. A missing or wrong secret: 401 and nothing is passed on.
Anything else, or any other host: 404.

Standard library only. The log prints route, method and status; never bodies or secrets.
"""

from __future__ import annotations

import hmac
import os
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ["TG_WEBHOOK_SECRET"].encode()
UPSTREAM = os.environ.get("BOT_SERVICES_URL", "http://bot-services:8100").rstrip("/")
PATH = "/telegram/webhook"
MAX_BODY = 64 * 1024
HOSTS = {"webhook.example.invalid", "tg-gateway"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: one route
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def log(*parts: object) -> None:
    print("tg-gateway", *parts, flush=True)


class Gateway(BaseHTTPRequestHandler):
    server_version = "tg-gateway"
    protocol_version = "HTTP/1.1"
    timeout = 10

    def log_message(self, *args: object) -> None:
        pass

    def reply(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        log("webhook", self.command, status)

    def route(self) -> None:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        path = self.path.split("?", 1)[0]
        if host not in HOSTS or self.command != "POST" or path != PATH:
            self.close_connection = True
            return self.reply(404, b"not found")
        given = (self.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").encode()
        if not hmac.compare_digest(given, SECRET):
            self.close_connection = True
            return self.reply(401, b"unauthorized")
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self.reply(413 if length > MAX_BODY else 411, b"bad length")
        body = self.rfile.read(length)
        req = urllib.request.Request(
            UPSTREAM + PATH,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": given.decode(),
            },
        )
        try:
            with OPENER.open(req, timeout=10) as resp:  # noqa: S310 (fixed internal URL)
                return self.reply(
                    resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
                )
        except urllib.error.HTTPError as exc:
            return self.reply(exc.code, exc.read(), "application/json")
        except OSError:
            return self.reply(502, b"bot services unavailable")

    do_GET = route
    do_POST = route
    do_HEAD = route
    do_PUT = route
    do_DELETE = route
    do_PATCH = route
    do_OPTIONS = route


def main() -> None:
    port = int(os.environ.get("GATEWAY_PORT", "443"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Gateway)  # noqa: S104 (container)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(
        os.environ.get("GATEWAY_CERT", "/certs/tg-gateway.crt"),
        os.environ.get("GATEWAY_KEY", "/certs/tg-gateway.key"),
    )
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    log(f"listening https:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
