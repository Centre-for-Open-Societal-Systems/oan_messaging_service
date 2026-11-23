"""OAN Bot Services HTTP API.

* Flow routes (``bot-services.openapi.yaml``): ``X-OAN-Flow-Secret`` compared in constant
  time; errors ``{error, text_key}``; a repeat ``request_id`` returns the first answer.
* Broadcast routes (the DA kit's OpenAPI): DA backend bearer token; errors
  ``application/problem+json`` with OAN Messaging's value-free codes; submitted values
  are never echoed.
* ``/healthz``: process up and database reachable.

    uvicorn --factory oan_bot_services.api.app:create_app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oan_bot_services import broadcasts, flows, logs
from oan_bot_services import text_keys as tk
from oan_bot_services.api import models as m
from oan_bot_services.deps import Deps, build
from oan_bot_services.errors import FlowError
from oan_bot_services.schema import request_replays
from oan_bot_services.settings import BotSettings, same_secret

log = logs.get(__name__)

PROBLEM_JSON = "application/problem+json"
MAX_BODY_BYTES = 64 * 1024
_TITLES = {401: "Unauthenticated", 404: "Not Found", 409: "Idempotency conflict",
           413: "Payload too large", 415: "Unsupported media type",
           422: "Validation failed", 503: "Service unavailable"}

Handler = Callable[[Deps, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]


def problem(status: int, code: str, errors: list[dict[str, str]] | None = None,
            headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": _TITLES.get(status, "Error"),
                            "status": status, "code": code}
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def flow_error(status: int, error: str, key: str) -> JSONResponse:
    return JSONResponse({"error": error, "text_key": key}, status_code=status)


def _validation_items(exc: ValidationError | RequestValidationError) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for err in exc.errors():
        etype = str(err.get("type", "invalid"))
        code = {"missing": "required", "extra_forbidden": "unknown_field",
                "string_pattern_mismatch": "invalid_format"}.get(etype, etype)
        loc = [str(p) for p in err.get("loc", ()) if p != "body"]
        item = {"code": code} if code == "unknown_field" else {
            "field": ".".join(loc) or "body", "code": code}
        if item not in items:
            items.append(item)
    return items


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        if deps is None:
            logs.install_process()
            app.state.deps = build(BotSettings())
        yield
        if deps is None:
            await app.state.deps.aclose()

    app = FastAPI(title="OAN Bot Services", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    if deps is not None:
        app.state.deps = deps

    def d() -> Deps:
        return app.state.deps  # type: ignore[no-any-return]

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/broadcasts"):
            return problem(422, "validation_failed", _validation_items(exc))
        return flow_error(422, "validation_failed", tk.COMMON_INVALID_CHOICE)

    @app.get("/healthz")
    async def health() -> JSONResponse:
        try:
            async with d().sf() as s:
                await s.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------- flow routes

    async def flow_call(
        request: Request, model: type[BaseModel], route: str, handler: Handler,
        *, replay: bool = True,
    ) -> JSONResponse:
        if not same_secret(request.headers.get("X-OAN-Flow-Secret"), d().settings.flow_secret):
            return flow_error(401, "unauthenticated", tk.COMMON_UNAVAILABLE)
        try:
            raw = json.loads(await request.body())
            body = model.model_validate(raw).model_dump(mode="json")
        except (ValueError, ValidationError):
            return flow_error(422, "validation_failed", tk.COMMON_INVALID_CHOICE)
        ref = flows.ref_of(d(), body["contact"])
        request_id = uuid.UUID(body["request_id"]) if replay else None
        if request_id is not None:
            async with d().sf() as s:
                prior = (
                    await s.execute(
                        select(request_replays).where(request_replays.c.request_id == request_id)
                    )
                ).mappings().first()
            if prior is not None:
                if prior["route"] != route or prior["contact_ref"] != ref:
                    return flow_error(422, "validation_failed", tk.COMMON_INVALID_CHOICE)
                return JSONResponse(prior["response"], status_code=prior["status_code"])
        try:
            status, out = await handler(d(), body)
        except FlowError as exc:
            return flow_error(exc.status, exc.error, exc.text_key)
        except Exception as exc:
            log.error("flow route failed route=%s contact_ref=%s error_type=%s",
                      route, ref, type(exc).__name__)
            return flow_error(503, "unavailable", tk.COMMON_UNAVAILABLE)
        if request_id is not None and status == 200:
            async with d().sf() as s, s.begin():
                stmt = pg_insert(request_replays).values(
                    request_id=request_id, route=route, contact_ref=ref, status_code=status,
                    response=out, created_at=await d().clock(),
                )
                await s.execute(stmt.on_conflict_do_nothing(index_elements=["request_id"]))
        return JSONResponse(out, status_code=status)

    routes: list[tuple[str, str, type[BaseModel], Handler, bool]] = [
        ("POST", "/contacts/location", m.LocationIn, flows.set_location, True),
        ("PUT", "/contacts/updates", m.UpdatesIn, flows.set_updates, True),
        ("POST", "/contacts/idle", m.IdleIn, flows.contact_idle, False),
        ("POST", "/contacts/stop", m.StopIn, flows.contact_stop, True),
        ("POST", "/staff/link", m.StaffLinkIn, flows.staff_link, True),
        ("POST", "/staff/inbound", m.StaffInboundIn, flows.staff_inbound, True),
        ("POST", "/farmer-link/start", m.FarmerLinkStartIn, flows.farmer_link_start, True),
        ("POST", "/farmer-link/verify", m.FarmerLinkVerifyIn, flows.farmer_link_verify, True),
        ("DELETE", "/farmer-link", m.UnlinkIn, flows.farmer_unlink, True),
    ]
    for method, path, model, handler, replay in routes:

        def endpoint(
            model: type[BaseModel] = model, path: str = path, handler: Handler = handler,
            replay: bool = replay,
        ) -> Callable[[Request], Awaitable[JSONResponse]]:
            async def call(request: Request) -> JSONResponse:
                return await flow_call(request, model, path, handler, replay=replay)

            return call

        app.add_api_route(path, endpoint(), methods=[method], include_in_schema=False)

    # -------------------------------------------------------- broadcast routes

    def authorised(request: Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return broadcasts.token_ok(token.strip(), d().settings.broadcast_digests())

    def as_problem(exc: broadcasts.Problem) -> JSONResponse:
        return problem(exc.status, exc.code, exc.errors, exc.headers)

    @app.post("/broadcasts")
    async def create_broadcast(request: Request) -> JSONResponse:
        if not authorised(request):
            return problem(401, "unauthenticated")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return problem(415, "unsupported_media_type")
        raw_bytes = await request.body()
        if len(raw_bytes) > MAX_BODY_BYTES:
            return problem(413, "payload_too_large")
        key = request.headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            return problem(422, "validation_failed",
                           [{"field": "header.Idempotency-Key", "code": "required" if not key
                             else "too_long"}])
        try:
            raw = json.loads(raw_bytes)
            if not isinstance(raw, dict):
                raise ValueError
        except ValueError:
            return problem(422, "validation_failed", [{"field": "body", "code": "invalid_json"}])
        try:
            req = m.BroadcastIn.model_validate(raw)
        except ValidationError as exc:
            return problem(422, "validation_failed", _validation_items(exc))
        try:
            status, out = await broadcasts.create(d(), key, raw, req.model_dump())
        except broadcasts.Problem as exc:
            return as_problem(exc)
        return JSONResponse(out, status_code=status)

    @app.get("/broadcasts/{broadcast_id}")
    async def get_broadcast(broadcast_id: str, request: Request) -> JSONResponse:
        if not authorised(request):
            return problem(401, "unauthenticated")
        try:
            return JSONResponse(await broadcasts.progress(d(), broadcast_id))
        except broadcasts.Problem as exc:
            return as_problem(exc)

    @app.get("/broadcasts/{broadcast_id}/telegram-recipients")
    async def get_recipients(broadcast_id: str, request: Request) -> JSONResponse:
        if not authorised(request):
            return problem(401, "unauthenticated")
        cursor = request.query_params.get("cursor")
        try:
            return JSONResponse(await broadcasts.telegram_recipients(d(), broadcast_id, cursor))
        except broadcasts.Problem as exc:
            return as_problem(exc)

    return app
