"""Runs last: every operation in every stub contract was called at least once and its
success answer validated against the contract."""

from __future__ import annotations

import pytest
from stubs_contract import CHECKED, LOADED


def test_every_contract_operation_was_checked():
    if not CHECKED:
        pytest.skip("run the whole folder: this test counts what the other tests checked")
    missing = []
    for name, contract in LOADED.items():
        for method, template, _ in contract.ops:
            ok = [
                s
                for (svc, m, t, s) in CHECKED
                if (svc, m, t) == (name, method, template) and s.startswith("2")
            ]
            if not ok:
                missing.append(f"{name} {method.upper()} {template}")
    assert not missing, "not checked: " + ", ".join(missing)
