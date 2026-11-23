"""OpenAPI checks for tests: requests our clients send and answers our routes give are
validated against the frozen contract files."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "telegram" / "contracts"
KIT_DOCS = ROOT / "integrations" / "kits" / "oan_da_app_services" / "broadcast-channels" / "docs"
FILES = {
    "bot": CONTRACTS / "bot-services.openapi.yaml",
    "broadcasts": KIT_DOCS / "bot-services-broadcasts.openapi.yaml",
    "da": CONTRACTS / "stubs" / "da-backend.openapi.yaml",
    "auth": CONTRACTS / "stubs" / "auth.openapi.yaml",
    "beneficiary": KIT_DOCS / "beneficiary-api.openapi.yaml",
    "master-data": CONTRACTS / "stubs" / "master-data.openapi.yaml",
    "marketplace": CONTRACTS / "stubs" / "marketplace.openapi.yaml",
}


@cache
def spec(name: str) -> dict[str, Any]:
    return dict(yaml.safe_load(FILES[name].read_text()))


def _deref(node: Any, doc: dict[str, Any], depth: int = 0) -> Any:
    if depth > 30:
        raise ValueError("ref too deep")
    if isinstance(node, dict):
        if "$ref" in node:
            target: Any = doc
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            return _deref(target, doc, depth + 1)
        return {k: _deref(v, doc, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(v, doc, depth + 1) for v in node]
    return node


def operation(name: str, method: str, path: str) -> tuple[str, dict[str, Any]] | None:
    """(template, operation) matching a concrete path, or None."""
    doc = spec(name)
    for template, item in doc["paths"].items():
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
        if re.match(pattern, path) and method.lower() in item:
            return template, _deref(item[method.lower()], doc)
    return None


def _validate(schema: dict[str, Any], value: Any) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [f"{'/'.join(map(str, e.path))}: {e.message}" for e in validator.iter_errors(value)]


def check_request(
    name: str, method: str, path: str, body: Any, query: dict[str, list[str]] | None = None
) -> list[str]:
    found = operation(name, method, path)
    if found is None:
        return [f"{name}: no operation {method} {path}"]
    template, op = found
    problems: list[str] = []
    rb = op.get("requestBody")
    if rb is not None:
        content = rb["content"]
        media = next(iter(content))
        if media == "application/json":
            problems += [
                f"{name} {template} body {p}" for p in _validate(content[media]["schema"], body)
            ]
    elif body not in (None, b"", ""):
        problems.append(f"{name} {template}: body not expected")
    allowed = {p["name"]: p for p in op.get("parameters", []) if p["in"] == "query"}
    for key, values in (query or {}).items():
        if key not in allowed:
            problems.append(f"{name} {template}: unknown query parameter {key}")
            continue
        schema = allowed[key]["schema"]
        if schema.get("type") == "array":
            problems += _validate(schema, values)
        else:
            for v in values:
                value: Any = v
                if schema.get("type") in ("number", "integer"):
                    value = float(v)
                problems += _validate(schema, value)
    for name_, p in allowed.items():
        if p.get("required") and name_ not in (query or {}):
            problems.append(f"{name} {template}: missing query parameter {name_}")
    return problems


def check_response(name: str, method: str, path: str, status: int, body: Any) -> list[str]:
    found = operation(name, method, path)
    if found is None:
        return [f"{name}: no operation {method} {path}"]
    template, op = found
    responses = op.get("responses", {})
    resp = responses.get(str(status))
    if resp is None:
        return [f"{name} {template}: status {status} not in the contract"]
    content = resp.get("content")
    if not content:
        return []
    media = next(iter(content))
    schema = content[media].get("schema")
    if schema is None:
        return []
    return [f"{name} {template} {status} {p}" for p in _validate(schema, body)]


def check_error(body: Any) -> list[str]:
    """``components.schemas.Error`` of ``bot-services.openapi.yaml``."""
    doc = spec("bot")
    return _validate(_deref(doc["components"]["schemas"]["Error"], doc), body)
