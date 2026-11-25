"""Fake Telegram Bot API for the local stack (contract: telegram/contracts/fake-telegram.md).

Standard library only. Two listeners:

* HTTPS on FAKE_TG_BOT_PORT (default 443) with the test-CA certificate for
  `api.telegram.org`: the Bot API (`/bot<token>/<method>`) and file downloads
  (`/file/bot<token>/<path>`).
* Plain HTTP on FAKE_TG_TEST_PORT (default 8301): the `/_test` routes a test uses to play
  a farmer and to read what the bot sent.

`POST /_test/telegram {"down": true}` closes the Bot API listener, so a sender gets
"connection refused", as when Telegram cannot be reached; `{"down": false}` opens it again.

Everything is kept in memory and cleared by `POST /_test/reset`. The log prints the method,
a keyed hash of the chat id and the status; never text, chat ids or tokens.
"""

from __future__ import annotations

import base64
import collections
import email.parser
import email.policy
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{35}$")
BOT_PATH_RE = re.compile(r"^/bot(?P<token>[^/]+)/(?P<method>[A-Za-z]+)$")
FILE_PATH_RE = re.compile(r"^/file/bot(?P<token>[^/]+)/(?P<path>.+)$")
SEND_METHODS = {"sendMessage", "sendPhoto", "sendDocument", "sendAudio", "sendVoice", "sendVideo"}
MEDIA_PARAM = {
    "sendPhoto": "photo",
    "sendDocument": "document",
    "sendAudio": "audio",
    "sendVoice": "voice",
    "sendVideo": "video",
}
MAX_TEXT_UNITS = 4096
GLOBAL_PER_SEC = int(os.environ.get("FAKE_TG_GLOBAL_PER_SEC", "30"))
CHAT_PER_SEC = int(os.environ.get("FAKE_TG_CHAT_PER_SEC", "1"))
TIMEOUT_HOLD_S = float(os.environ.get("FAKE_TG_TIMEOUT_HOLD_S", "30"))
CA_FILE = os.environ.get("FAKE_TG_CA_FILE", "/certs/ca.crt")
LOG_KEY = secrets.token_bytes(32)  # per process: log hashes cannot be joined across runs

BOT = {
    "id": 7000000001,
    "is_bot": True,
    "first_name": "OAN Test Bot",
    "username": "oan_test_bot",
    "can_join_groups": False,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}

# Small sample files a test can attach by name.
SAMPLES = {
    "sample.jpg": (
        "image/jpeg",
        base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP////////////////////////////////////////////"
            "//////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAA"
            "AAAAAAAAAP/aAAgBAQABPxA="
        ),
    ),
    "sample.ogg": ("audio/ogg", b"OggS" + bytes(60)),
    "sample.pdf": ("application/pdf", b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"),
}
FAULTS = {
    "blocked": (403, "Forbidden: bot was blocked by the user", True),
    "deactivated": (403, "Forbidden: user is deactivated", True),
    "not_found": (400, "Bad Request: chat not found", True),
    "rate_limited": (429, "Too Many Requests: retry after 3", False),
    "timeout": (None, None, False),
}


def ref(chat_id: object) -> str:
    return hmac.new(LOG_KEY, str(chat_id).encode(), hashlib.sha256).hexdigest()[:12]


def log(*parts: object) -> None:
    print("fake-telegram", *parts, flush=True)


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class State:
    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.reset()

    def reset(self) -> None:
        self.users: dict[int, dict] = {}
        self.sent: dict[int, list[dict]] = collections.defaultdict(list)
        self.cursor: dict[int, int] = {}
        self.faults: dict[int, str] = {}
        self.files: dict[str, dict] = {}
        self.msg_ids: dict[int, itertools.count] = collections.defaultdict(
            lambda: itertools.count(1)
        )
        self.global_sends: collections.deque[float] = collections.deque()
        self.chat_sends: dict[int, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        # Update ids never repeat, across resets and restarts: OAN Bot Services keeps each
        # update_id for 7 days and ignores a repeat, as Telegram expects.
        if not hasattr(self, "update_ids"):
            self.update_ids = itertools.count(int(time.time()) * 100)
        # The webhook registration survives a reset: it belongs to the stack, not the test.
        if not hasattr(self, "webhook"):
            self.webhook = {
                "url": "",
                "secret_token": "",
                "allowed_updates": [],
                "max_connections": 40,
                "pending_update_count": 0,
                "last_error_date": None,
                "last_error_message": None,
            }


STATE = State()


def ca_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=CA_FILE)


def tg_ok(result: object) -> tuple[int, dict]:
    return 200, {"ok": True, "result": result}


def tg_err(code: int, description: str, **extra: object) -> tuple[int, dict]:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if extra:
        body["parameters"] = extra
    return code, body


def parse_params(handler: BaseHTTPRequestHandler, body: bytes) -> tuple[dict, dict]:
    """Return (fields, uploads) from the query string and a JSON, form or multipart body."""
    fields: dict = {
        k: v[-1]
        for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).items()
    }
    uploads: dict = {}
    ctype = handler.headers.get("Content-Type", "")
    if not body:
        return fields, uploads
    if ctype.startswith("application/json"):
        data = json.loads(body or b"{}")
        fields.update({k: v for k, v in data.items()})
    elif ctype.startswith("application/x-www-form-urlencoded"):
        fields.update({k: v[-1] for k, v in urllib.parse.parse_qs(body.decode()).items()})
    elif ctype.startswith("multipart/form-data"):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
        )
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                uploads[name] = {
                    "filename": part.get_filename(),
                    "bytes": len(part.get_payload(decode=True) or b""),
                }
            else:
                fields[name] = part.get_content()
    return fields, uploads


def json_field(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def keyboard_labels(markup: object) -> list[list[str]]:
    if not isinstance(markup, dict):
        return []
    rows = markup.get("keyboard") or markup.get("inline_keyboard") or []
    out = []
    for row in rows:
        out.append([b if isinstance(b, str) else b.get("text", "") for b in row])
    return out


def rate_limited(chat_id: int) -> int | None:
    """Return retry_after seconds when the bot or the chat is over Telegram's limits."""
    now = time.monotonic()
    g, c = STATE.global_sends, STATE.chat_sends[chat_id]
    for q in (g, c):
        while q and now - q[0] >= 1.0:
            q.popleft()
    if len(g) >= GLOBAL_PER_SEC or len(c) >= CHAT_PER_SEC:
        oldest = min([q[0] for q in (g, c) if q], default=now)
        return max(1, int(1.0 - (now - oldest) + 0.999))
    g.append(now)
    c.append(now)
    return None


def fetch_media(url: str) -> dict:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "fake-telegram"})
        ctx = ca_context() if url.startswith("https:") else None
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # noqa: S310 (test fake)
            data = resp.read(20 * 1024 * 1024)
            return {
                "status": resp.status,
                "bytes": len(data),
                "content_type": resp.headers.get("Content-Type", ""),
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "bytes": 0, "content_type": ""}
    except Exception as exc:  # noqa: BLE001 (record any failure)
        return {"status": None, "bytes": 0, "content_type": "", "error": type(exc).__name__}


def bot_method(method: str, fields: dict, uploads: dict) -> tuple[int, dict] | None:
    """Handle one Bot API call. Returns None to drop the connection (timeout fault)."""
    if method == "getMe":
        return tg_ok(BOT)
    if method == "setWebhook":
        allowed = json_field(fields.get("allowed_updates", [])) or []
        with STATE.lock:
            STATE.webhook.update(
                url=str(fields.get("url", "")),
                secret_token=str(fields.get("secret_token", "")),
                allowed_updates=allowed,
                max_connections=int(fields.get("max_connections", 40)),
                last_error_date=None,
                last_error_message=None,
            )
        log("setWebhook", "secret=" + ("set" if fields.get("secret_token") else "none"))
        return tg_ok(True)
    if method == "deleteWebhook":
        with STATE.lock:
            STATE.webhook.update(url="", secret_token="", allowed_updates=[])
        return tg_ok(True)
    if method == "getWebhookInfo":
        with STATE.lock:
            info = {k: v for k, v in STATE.webhook.items() if v is not None}
        info["has_custom_certificate"] = False
        return tg_ok(info)
    if method == "getFile":
        f = STATE.files.get(str(fields.get("file_id", "")))
        if not f:
            return tg_err(400, "Bad Request: invalid file_id")
        return tg_ok(
            {
                "file_id": f["file_id"],
                "file_unique_id": f["file_unique_id"],
                "file_size": len(f["data"]),
                "file_path": f["file_path"],
            }
        )
    if method == "sendChatAction":
        return tg_ok(True)
    if method not in SEND_METHODS:
        return tg_err(404, "Not Found: method not found")

    try:
        chat_id = int(str(fields.get("chat_id", "")))
    except ValueError:
        return tg_err(400, "Bad Request: chat not found")
    with STATE.lock:
        fault = STATE.faults.get(chat_id)
        if fault:
            code, desc, sticky = FAULTS[fault]
            if not sticky:
                STATE.faults.pop(chat_id, None)
    if fault == "timeout":
        time.sleep(TIMEOUT_HOLD_S)
        return None
    if fault == "rate_limited":
        return tg_err(429, desc, retry_after=3)
    if fault:
        return tg_err(code, desc)

    text = fields.get("text")
    if method == "sendMessage":
        if not text:
            return tg_err(400, "Bad Request: message text is empty")
        if utf16_units(str(text)) > MAX_TEXT_UNITS:
            return tg_err(400, "Bad Request: message is too long")
    with STATE.lock:
        if chat_id not in STATE.users:
            return tg_err(400, "Bad Request: chat not found")
        retry = rate_limited(chat_id)
    if retry is not None:
        return tg_err(429, f"Too Many Requests: retry after {retry}", retry_after=retry)

    record: dict = {
        "method": method,
        "chat_id": chat_id,
        "text": text,
        "caption": fields.get("caption"),
        "parse_mode": fields.get("parse_mode"),
        "reply_markup": json_field(fields.get("reply_markup")),
        "link_preview_options": json_field(fields.get("link_preview_options")),
        "at": time.time(),
    }
    record["buttons"] = keyboard_labels(record["reply_markup"])
    if method in MEDIA_PARAM:
        param = MEDIA_PARAM[method]
        if param in uploads:
            record["media_upload"] = uploads[param]
        else:
            media = str(fields.get(param, ""))
            record["media_url"] = media
            if media.startswith(("http://", "https://")):
                record["media_fetch"] = fetch_media(media)
                if record["media_fetch"]["status"] != 200:
                    return tg_err(400, "Bad Request: failed to get HTTP URL content")
            elif media not in STATE.files:
                return tg_err(400, "Bad Request: wrong file identifier/HTTP URL specified")
    with STATE.lock:
        record["message_id"] = next(STATE.msg_ids[chat_id])
        STATE.sent[chat_id].append(record)
        STATE.lock.notify_all()
        user = STATE.users[chat_id]
    result = {
        "message_id": record["message_id"],
        "date": int(record["at"]),
        "chat": {"id": chat_id, "type": "private", "first_name": user["first_name"]},
        "from": {k: BOT[k] for k in ("id", "is_bot", "first_name", "username")},
    }
    if text:
        result["text"] = text
    return tg_ok(result)


def build_update(user: dict, body: dict) -> dict | tuple[int, dict]:
    chat_id = user["id"]
    with STATE.lock:
        message_id = next(STATE.msg_ids[chat_id])
        update_id = next(STATE.update_ids)
    frm = {
        "id": chat_id,
        "is_bot": False,
        "first_name": user["first_name"],
        "language_code": user["language_code"],
    }
    msg: dict = {
        "message_id": message_id,
        "from": frm,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private", "first_name": user["first_name"]},
    }
    if "text" in body:
        msg["text"] = str(body["text"])
        if msg["text"].startswith("/"):
            cmd = msg["text"].split()[0]
            msg["entities"] = [{"offset": 0, "length": utf16_units(cmd), "type": "bot_command"}]
    elif "contact" in body:
        c = body["contact"] or {}
        msg["contact"] = {
            "phone_number": str(c.get("phone_number", "")),
            "first_name": user["first_name"],
            "user_id": int(c.get("user_id", chat_id)),
        }
    else:
        kind = next((k for k in ("photo", "voice", "document") if k in body), None)
        if kind is None:
            return 400, {"error": "text, contact, photo, voice or document is required"}
        spec = body[kind]
        if isinstance(spec, str):
            if spec not in SAMPLES:
                return 400, {"error": f"unknown test file; use one of {sorted(SAMPLES)}"}
            mime, data = SAMPLES[spec]
            fname = spec
        else:
            mime = spec.get("mime_type", "application/octet-stream")
            data = base64.b64decode(spec.get("content_base64", ""))
            fname = spec.get("file_name", "file.bin")
        file_id = "F" + secrets.token_urlsafe(24)
        ext = fname.rsplit(".", 1)[-1] if "." in fname else "bin"
        folder = {"photo": "photos", "voice": "voice", "document": "documents"}[kind]
        f = {
            "file_id": file_id,
            "file_unique_id": "U" + secrets.token_urlsafe(8),
            "file_path": f"{folder}/file_{update_id}.{ext}",
            "data": data,
            "mime_type": mime,
        }
        with STATE.lock:
            STATE.files[file_id] = f
        if kind == "photo":
            msg["photo"] = [
                {
                    "file_id": file_id,
                    "file_unique_id": f["file_unique_id"],
                    "width": 1,
                    "height": 1,
                    "file_size": len(data),
                }
            ]
        elif kind == "voice":
            msg["voice"] = {
                "file_id": file_id,
                "file_unique_id": f["file_unique_id"],
                "duration": 1,
                "mime_type": mime,
                "file_size": len(data),
            }
        else:
            msg["document"] = {
                "file_id": file_id,
                "file_unique_id": f["file_unique_id"],
                "file_name": fname,
                "mime_type": mime,
                "file_size": len(data),
            }
        if body.get("caption"):
            msg["caption"] = str(body["caption"])
    return {"update_id": update_id, "message": msg}


def post_update(update: dict) -> tuple[int, dict]:
    with STATE.lock:
        hook = dict(STATE.webhook)
    if not hook["url"]:
        return 409, {"error": "no webhook registered"}
    if hook["allowed_updates"] and "message" not in hook["allowed_updates"]:
        return 200, {"ok": True, "webhook_status": None, "skipped": "allowed_updates"}
    headers = {"Content-Type": "application/json"}
    if hook["secret_token"]:
        headers["X-Telegram-Bot-Api-Secret-Token"] = hook["secret_token"]
    req = urllib.request.Request(
        hook["url"], data=json.dumps(update).encode(), headers=headers, method="POST"
    )
    ctx = ca_context() if hook["url"].startswith("https:") else None
    status, error = None, None
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # noqa: S310 (test fake)
            status = resp.status
    except urllib.error.HTTPError as exc:
        status, error = exc.code, f"Wrong response from the webhook: {exc.code}"
    except Exception as exc:  # noqa: BLE001
        error = f"Connection failed: {type(exc).__name__}"
    with STATE.lock:
        if error:
            STATE.webhook.update(last_error_date=int(time.time()), last_error_message=error)
            STATE.webhook["pending_update_count"] += 1
    log("update", "ref=" + ref(update["message"]["chat"]["id"]), "webhook_status=" + str(status))
    if status is None:
        return 502, {"ok": False, "error": error}
    return 200, {
        "ok": True,
        "webhook_status": status,
        "update_id": update["update_id"],
        "message_id": update["message"]["message_id"],
    }


def view(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "chat_id"} | {"telegram_id": rec["chat_id"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-telegram"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # the default would print paths with tokens
        pass

    def send_json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""


OPEN: set[socket.socket] = set()  # Bot API connections, closed when Telegram goes down


class BotHandler(Handler):
    def setup(self) -> None:
        super().setup()
        with BOT_LOCK:
            OPEN.add(self.connection)

    def finish(self) -> None:
        with BOT_LOCK:
            OPEN.discard(self.connection)
        super().finish()

    def handle_bot(self) -> None:
        body = self.read_body()
        path = urllib.parse.urlsplit(self.path).path
        m = FILE_PATH_RE.match(path)
        if m and self.command == "GET":
            if not TOKEN_RE.match(m["token"]):
                return self.send_json(
                    404, {"ok": False, "error_code": 404, "description": "Not Found"}
                )
            f = next((f for f in STATE.files.values() if f["file_path"] == m["path"]), None)
            if not f:
                return self.send_json(
                    404, {"ok": False, "error_code": 404, "description": "Not Found"}
                )
            self.send_response(200)
            self.send_header("Content-Type", f["mime_type"])
            self.send_header("Content-Length", str(len(f["data"])))
            self.end_headers()
            self.wfile.write(f["data"])
            log("file", "status=200")
            return None
        m = BOT_PATH_RE.match(path)
        if not m:
            return self.send_json(404, {"ok": False, "error_code": 404, "description": "Not Found"})
        if not TOKEN_RE.match(m["token"]):
            return self.send_json(*tg_err(401, "Unauthorized"))
        try:
            fields, uploads = parse_params(self, body)
        except ValueError:
            return self.send_json(*tg_err(400, "Bad Request: can't parse request"))
        result = bot_method(m["method"], fields, uploads)
        if result is None:
            log(m["method"], "ref=" + ref(fields.get("chat_id")), "status=timeout")
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return None
        status, payload = result
        if m["method"] in SEND_METHODS:
            log(m["method"], "ref=" + ref(fields.get("chat_id")), f"status={status}")
        return self.send_json(status, payload)

    do_GET = handle_bot
    do_POST = handle_bot


class TestHandler(Handler):
    def query(self) -> dict:
        return {
            k: v[-1]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()
        }

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        q = self.query()
        if path == "/_test/health":
            return self.send_json(200, {"ok": True})
        if path == "/_test/webhook":
            with STATE.lock:
                hook = STATE.webhook
                return self.send_json(
                    200,
                    {
                        "registered": bool(hook["url"]),
                        "secret": bool(hook["secret_token"]),
                        "allowed_updates": hook["allowed_updates"],
                    },
                )
        if path in ("/_test/sent", "/_test/wait"):
            try:
                tid = int(q["telegram_id"])
            except (KeyError, ValueError):
                return self.send_json(400, {"error": "telegram_id is required"})
            after = int(q["after"]) if q.get("after") else None
            if path == "/_test/sent":
                with STATE.lock:
                    msgs = [
                        view(r)
                        for r in STATE.sent.get(tid, [])
                        if after is None or r["message_id"] > after
                    ]
                return self.send_json(200, {"ok": True, "messages": msgs})
            count = int(q.get("count", "1"))
            timeout = int(q.get("timeout_ms", "10000")) / 1000
            deadline = time.monotonic() + timeout
            with STATE.lock:
                start = after if after is not None else STATE.cursor.get(tid, 0)

                def new() -> list[dict]:
                    return [r for r in STATE.sent.get(tid, []) if r["message_id"] > start]

                while len(new()) < count and time.monotonic() < deadline:
                    STATE.lock.wait(max(0.0, deadline - time.monotonic()))
                msgs = new()
                if len(msgs) >= count:
                    if after is None:
                        STATE.cursor[tid] = msgs[count - 1]["message_id"]
                    return self.send_json(
                        200, {"ok": True, "messages": [view(r) for r in msgs[:count]]}
                    )
            return self.send_json(
                408, {"ok": False, "error": "timeout", "messages": [view(r) for r in msgs]}
            )
        return self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = json.loads(self.read_body() or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "invalid JSON"})
        if path == "/_test/reset":
            with STATE.lock:
                STATE.reset()
            return self.send_json(200, {"ok": True})
        if path == "/_test/users":
            try:
                tid = int(body["telegram_id"])
            except (KeyError, ValueError):
                return self.send_json(400, {"error": "telegram_id is required"})
            user = {
                "id": tid,
                "first_name": str(body.get("first_name") or "Test"),
                "language_code": str(body.get("language_code") or "en"),
            }
            with STATE.lock:
                STATE.users[tid] = user
                STATE.cursor.setdefault(tid, 0)
            return self.send_json(201, {"ok": True, "user": user})
        if path == "/_test/updates":
            try:
                tid = int(body["telegram_id"])
            except (KeyError, ValueError):
                return self.send_json(400, {"error": "telegram_id is required"})
            with STATE.lock:
                user = STATE.users.get(tid)
                # Replies to this update are what a test waits for next.
                if user:
                    STATE.cursor[tid] = max(
                        [r["message_id"] for r in STATE.sent.get(tid, [])], default=0
                    )
            if not user:
                return self.send_json(404, {"error": "unknown user; POST /_test/users first"})
            update = build_update(user, body)
            if isinstance(update, tuple):
                return self.send_json(*update)
            return self.send_json(*post_update(update))
        if path == "/_test/telegram":
            if body.get("down"):
                stop_bot()
            else:
                start_bot()
            return self.send_json(200, {"ok": True, "down": BOT_SERVER is None})
        if path == "/_test/faults":
            mode = str(body.get("mode", ""))
            try:
                tid = int(body["telegram_id"])
            except (KeyError, ValueError):
                return self.send_json(400, {"error": "telegram_id is required"})
            if mode not in FAULTS and mode != "none":
                return self.send_json(
                    400, {"error": f"mode must be one of {sorted(FAULTS) + ['none']}"}
                )
            with STATE.lock:
                if mode == "none":
                    STATE.faults.pop(tid, None)
                else:
                    STATE.faults[tid] = mode
            return self.send_json(200, {"ok": True})
        return self.send_json(404, {"error": "not found"})


def serve_tls(port: int, cert: str, key: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("0.0.0.0", port), BotHandler)  # noqa: S104 (container)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


BOT_SERVER: ThreadingHTTPServer | None = None
BOT_LOCK = threading.Lock()


def start_bot() -> None:
    global BOT_SERVER
    with BOT_LOCK:
        if BOT_SERVER is not None:
            return
        BOT_SERVER = serve_tls(
            int(os.environ.get("FAKE_TG_BOT_PORT", "443")),
            os.environ.get("FAKE_TG_CERT", "/certs/fake-telegram.crt"),
            os.environ.get("FAKE_TG_KEY", "/certs/fake-telegram.key"),
        )
        threading.Thread(target=BOT_SERVER.serve_forever, daemon=True).start()
    log("bot api up")


def stop_bot() -> None:
    global BOT_SERVER
    with BOT_LOCK:
        srv, BOT_SERVER = BOT_SERVER, None
    if srv is not None:
        srv.shutdown()
        srv.server_close()
    # Kept-alive connections too, so a sender's next request has to connect and is refused.
    with BOT_LOCK:
        conns = list(OPEN)
    for c in conns:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    log("bot api down")


def main() -> None:
    bot_port = int(os.environ.get("FAKE_TG_BOT_PORT", "443"))
    test_port = int(os.environ.get("FAKE_TG_TEST_PORT", "8301"))
    ThreadingHTTPServer.allow_reuse_address = True
    test = ThreadingHTTPServer(("0.0.0.0", test_port), TestHandler)  # noqa: S104 (container)
    start_bot()
    log(f"listening bot=https:{bot_port} test=http:{test_port}")
    test.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
