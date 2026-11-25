"""Fake 8028 SMS gateway for the local stack. Standard library only.

Two listeners:

* HTTPS with mutual TLS on FAKE_8028_PORT (default 3400), certificate for
  `sms8028.example.invalid` from the stack's test CA. A client certificate from the same
  CA is required. Route: `POST /sms-api/v1/sms/send-sms`, the body OAN Messaging's 8028
  adapter sends (`clientSecret`, `smstext`, `phonenums`, `pushdatetime`). The client
  secret must match FAKE_8028_CLIENT_SECRET (constant-time compare). The answer has
  8028's shape: `{"success": true, "data": [{"phone_number", "sms_id"}, ...]}`, one entry
  per number, in order.
* Plain HTTP on FAKE_8028_TEST_PORT (default 8302), inside the stack only:
  `GET /_test/sent?phone=` (what arrived, with the arrival time), `POST /_test/reset`,
  `GET /_test/health`.

Kept in memory. The log prints the route and status; never text, numbers or secrets.
"""

from __future__ import annotations

import hmac
import itertools
import json
import os
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ["FAKE_8028_CLIENT_SECRET"].encode()
SEND_PATH = "/sms-api/v1/sms/send-sms"
LOCK = threading.Lock()
SENT: list[dict] = []
IDS = itertools.count(500001)


def log(*parts: object) -> None:
    print("fake-8028", *parts, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-8028"

    def log_message(self, *args: object) -> None:
        pass

    def send_json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""


class GatewayHandler(Handler):
    def do_POST(self) -> None:
        raw = self.body()
        if urllib.parse.urlsplit(self.path).path != SEND_PATH:
            log("send", 404)
            return self.send_json(404, {"success": False, "message": "not found"})
        try:
            req = json.loads(raw)
        except ValueError:
            log("send", 400)
            return self.send_json(400, {"success": False, "message": "invalid JSON"})
        given = str(req.get("clientSecret", "")).encode()
        if not hmac.compare_digest(given, SECRET):
            log("send", 401)
            return self.send_json(401, {"success": False, "message": "unauthorized"})
        nums = req.get("phonenums")
        if not isinstance(nums, list) or not nums or not req.get("smstext"):
            log("send", 400)
            return self.send_json(400, {"success": False, "message": "bad request"})
        data = []
        with LOCK:
            for n in nums:
                sms_id = str(next(IDS))
                SENT.append(
                    {"phone": str(n), "text": req["smstext"], "sms_id": sms_id, "at": time.time()}
                )
                data.append({"phone_number": str(n), "sms_id": sms_id})
        log("send", 201, f"numbers={len(nums)}")
        return self.send_json(201, {"success": True, "message": "queued", "data": data})


class TestHandler(Handler):
    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/_test/health":
            return self.send_json(200, {"ok": True})
        if parts.path == "/_test/sent":
            q = dict(urllib.parse.parse_qsl(parts.query))
            with LOCK:
                rows = [r for r in SENT if not q.get("phone") or r["phone"] == q["phone"]]
            return self.send_json(200, {"ok": True, "messages": rows})
        return self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.body()
        if urllib.parse.urlsplit(self.path).path == "/_test/reset":
            with LOCK:
                SENT.clear()
            return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": "not found"})


def main() -> None:
    port = int(os.environ.get("FAKE_8028_PORT", "3400"))
    test_port = int(os.environ.get("FAKE_8028_TEST_PORT", "8302"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), GatewayHandler)  # noqa: S104 (container)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(
        os.environ.get("FAKE_8028_CERT", "/certs/fake-8028.crt"),
        os.environ.get("FAKE_8028_KEY", "/certs/fake-8028.key"),
    )
    ctx.load_verify_locations(os.environ.get("FAKE_8028_CA", "/certs/ca.crt"))
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS, as 8028
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    test = ThreadingHTTPServer(("0.0.0.0", test_port), TestHandler)  # noqa: S104 (container)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"listening https:{port} (mutual TLS) test=http:{test_port}")
    test.serve_forever()


if __name__ == "__main__":
    main()
