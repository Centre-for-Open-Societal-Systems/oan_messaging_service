"""Fixtures: a real stubs server on a free port (frozen clock), reset before every test."""

# ruff: noqa: E402, I001
from __future__ import annotations

import secrets
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # telegram/stubs

import pytest
import stubs_server
from stubs_contract import StubClient


@pytest.fixture(scope="session")
def server():
    client_id, client_secret = "test-client", secrets.token_urlsafe(16)
    state = stubs_server.State(
        stubs_server.load_seed(), client_id, client_secret, stubs_server.Clock("frozen")
    )
    srv = stubs_server.make_server("127.0.0.1", 0, state)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, state, client_id, client_secret
    srv.shutdown()


@pytest.fixture
def stub(server):
    srv, state, client_id, client_secret = server
    state.reset()
    state.slow_seconds = stubs_server.DEFAULT_SLOW_SECONDS
    host, port = srv.server_address[:2]
    c = StubClient(f"http://{host}:{port}", client_id, client_secret)
    c.state = state
    yield c
    c.http.close()
