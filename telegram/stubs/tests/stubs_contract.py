"""Contract checking for the stubs tests: loads the frozen OpenAPI files and checks every
request and response a test sends through StubClient."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

STUBS = Path(__file__).resolve().parents[1]
REPO = STUBS.parents[1]


CONTRACTS = {
    "da": REPO / "telegram/contracts/stubs/da-backend.openapi.yaml",
    "auth": REPO / "telegram/contracts/stubs/auth.openapi.yaml",
    "beneficiary": REPO
    / "integrations/kits/oan_da_app_services/broadcast-channels/docs/beneficiary-api.openapi.yaml",
    "master-data": REPO / "telegram/contracts/stubs/master-data.openapi.yaml",
    "marketplace": REPO / "telegram/contracts/stubs/marketplace.openapi.yaml",
}


class Contract:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.uri = f"urn:contract:{name}"
        self.registry = Registry().with_resource(
            self.uri, Resource.from_contents(self.doc, default_specification=DRAFT202012)
        )
        self.ops = []  # (method, template, regex)
        for template, item in self.doc["paths"].items():
            rx = re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$")
            for method in item:
                if method in ("get", "post", "put", "delete"):
                    self.ops.append((method, template, rx))

    def match(self, method: str, path: str):
        for m, template, rx in self.ops:
            if m == method and rx.match(path):
                return template
        raise AssertionError(f"{method.upper()} {path} is not in the {self.name} contract")

    def validate(self, pointer: str, instance) -> None:
        v = Draft202012Validator(
            {"$ref": f"{self.uri}#{pointer}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(v.iter_errors(instance), key=lambda e: e.path)
        assert not errors, f"{self.name}{pointer}: " + "; ".join(e.message for e in errors[:5])

    def schema_pointer(self, template: str, method: str, *parts: str) -> str | None:
        node = self.doc["paths"][template][method]
        ptr = "/paths/" + _esc(template) + "/" + method
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
            ptr += "/" + _esc(p)
        return ptr


def _esc(s: str) -> str:
    return s.replace("~", "~0").replace("/", "~1")


LOADED = {name: Contract(name, path) for name, path in CONTRACTS.items()}
CHECKED: set = set()  # (service, method, template, status) seen with a validated response


class StubClient:
    """httpx client that checks traffic to /<service>/... against the contract."""

    def __init__(self, base: str, client_id: str, client_secret: str):
        self.base = base
        self.http = httpx.Client(base_url=base, timeout=15)
        self.client_id, self.client_secret = client_id, client_secret
        self._token = None

    def token(self) -> str:
        if not self._token:
            r = self.call(
                "POST",
                "/auth/token",
                auth=False,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            assert r.status_code == 200, r.text
            self._token = r.json()["access_token"]
        return self._token

    def raw(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, **kw)

    def call(
        self, method: str, path: str, *, auth: bool = True, check_request: bool = True, **kw
    ) -> httpx.Response:
        headers = kw.pop("headers", {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        svc, sub = path.lstrip("/").split("/", 1)
        sub = "/" + sub.split("?")[0]
        contract = LOADED[svc]
        template = contract.match(method.lower(), sub)
        if check_request and "json" in kw:
            ptr = contract.schema_pointer(
                template, method.lower(), "requestBody", "content", "application/json", "schema"
            )
            assert ptr, f"no JSON request body in contract for {method} {template}"
            contract.validate(ptr, kw["json"])
        r = self.http.request(method, path, headers=headers, **kw)
        status = str(r.status_code)
        responses = contract.doc["paths"][template][method.lower()]["responses"]
        if status in responses:
            resp = responses[status]
            ptr = contract.schema_pointer(template, method.lower(), "responses", status)
            if "$ref" in resp:  # shared response object (beneficiary Problem)
                name = resp["$ref"].split("/")[-1]
                resp = contract.doc["components"]["responses"][name]
                ptr = "/components/responses/" + name
            content = resp.get("content") or {}
            if content:
                ctype = r.headers.get("content-type", "").split(";")[0]
                assert ctype in content, f"{method} {template} {status}: content type {ctype}"
                contract.validate(ptr + "/content/" + _esc(ctype) + "/schema", r.json())
            CHECKED.add((svc, method.lower(), template, status))
        return r
