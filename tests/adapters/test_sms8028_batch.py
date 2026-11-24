"""8028 batching in the adapter (``Sms8028Adapter.send_batch``).

Request and response format: guide v1.1 §5.1-5.2 (one ``smstext``, ``phonenums`` list;
``data`` has one entry per number, in the order sent, each with its own ``sms_id``).
413: 12-page guide §4.4 ("split into smaller batches"). Every call is mocked.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from test_sms8028 import SECRET, adapter_for, raising, req, respond

from oan_messaging.adapters.sms8028 import BATCH_TOO_LARGE
from oan_messaging.contracts import FailureCode, ProviderOutcome

TEXT = "Rain expected tomorrow in your kebele."
NUMBERS = ["+251900000101", "+251900000102", "+251900000103"]
DOMESTIC = ["0900000101", "0900000102", "0900000103"]


def batch_body(ids: list[Any], phones: list[str] = DOMESTIC) -> dict[str, Any]:
    return {
        "success": True,
        "message": "SMS records queued successfully.",
        "data": [{"sms_id": i, "phone_number": p} for i, p in zip(ids, phones, strict=True)],
    }


def batch() -> list[Any]:
    return [req(text=TEXT, recipient=n) for n in NUMBERS]


async def test_one_request_carries_every_number_and_ids_map_back_in_order() -> None:
    adapter, rec = adapter_for(respond(201, batch_body(["17006100", 17006101, "17006102"])))
    requests = batch()
    results = await adapter.send_batch(requests)
    assert len(rec.requests) == 1
    body = json.loads(rec.requests[0].content)
    assert body["phonenums"] == DOMESTIC and body["smstext"] == TEXT
    assert body["clientSecret"] == SECRET and body["pushdatetime"]
    assert [r.outcome for r in results] == [ProviderOutcome.SENT] * 3
    # Each message keeps its own sms_id; a numeric id is kept as text.
    assert [r.provider_message_id for r in results] == ["17006100", "17006101", "17006102"]
    assert all(r.provider_status == "http_201" for r in results)


async def test_200_is_read_like_201() -> None:
    adapter, _ = adapter_for(respond(200, batch_body(["1", "2", "3"])))
    results = await adapter.send_batch(batch())
    assert [r.provider_message_id for r in results] == ["1", "2", "3"]


async def test_duplicate_numbers_are_matched_by_position() -> None:
    # v1.1 §5.3: duplicates are not removed; each entry is its own message.
    same = ["+251900000101", "+251900000101"]
    adapter, rec = adapter_for(
        respond(201, batch_body(["11", "12"], ["0900000101", "0900000101"]))
    )
    results = await adapter.send_batch([req(text=TEXT, recipient=n) for n in same])
    assert json.loads(rec.requests[0].content)["phonenums"] == ["0900000101", "0900000101"]
    assert [r.provider_message_id for r in results] == ["11", "12"]


async def test_a_response_that_cannot_be_matched_makes_every_message_unknown() -> None:
    cases = [
        batch_body(["1", "2"], DOMESTIC[:2]),  # one entry missing
        batch_body(["1", "2", "3"], [DOMESTIC[1], DOMESTIC[0], DOMESTIC[2]]),  # out of order
        batch_body(["1", "1", "3"]),  # one sms_id twice
        batch_body(["1", "", "3"]),  # one sms_id invalid
        {"success": False, "data": []},
    ]
    for body in cases:
        adapter, _ = adapter_for(respond(201, body))
        results = await adapter.send_batch(batch())
        assert {r.outcome for r in results} == {ProviderOutcome.UNKNOWN}, body
        assert {r.failure_code for r in results} == {FailureCode.MALFORMED_RESPONSE}
        assert not any(r.retryable for r in results)


async def test_timeout_makes_every_message_unknown_never_retried() -> None:
    adapter, _ = adapter_for(raising(httpx.ReadTimeout("slow")))
    results = await adapter.send_batch(batch())
    assert {r.outcome for r in results} == {ProviderOutcome.UNKNOWN}
    assert {r.failure_code for r in results} == {FailureCode.TIMEOUT}
    assert not any(r.retryable for r in results)


async def test_500_means_nothing_queued_so_every_message_is_retried() -> None:
    adapter, _ = adapter_for(respond(500, {"success": False}))
    results = await adapter.send_batch(batch())
    assert {r.outcome for r in results} == {ProviderOutcome.NOT_SENT}
    assert all(r.retryable for r in results)


async def test_413_retries_every_message_one_by_one() -> None:
    adapter, _ = adapter_for(respond(413, {"success": False}))
    results = await adapter.send_batch(batch())
    assert {r.outcome for r in results} == {ProviderOutcome.NOT_SENT}
    assert all(r.retryable and r.provider_status == "http_413" for r in results)
    assert {r.detail for r in results} == {BATCH_TOO_LARGE}


async def test_400_rejects_the_whole_request() -> None:
    adapter, _ = adapter_for(respond(400, {"success": False, "message": "Invalid phone number"}))
    results = await adapter.send_batch(batch())
    assert {r.outcome for r in results} == {ProviderOutcome.REJECTED}


async def test_a_bad_request_is_left_out_and_the_rest_are_sent() -> None:
    adapter, rec = adapter_for(respond(201, batch_body(["1", "3"], [DOMESTIC[0], DOMESTIC[2]])))
    requests = batch()
    # A control character: refused before the call, so this number is left out.
    requests[1] = req(text=TEXT + "\x01", recipient=NUMBERS[1])
    results = await adapter.send_batch(requests)
    assert json.loads(rec.requests[0].content)["phonenums"] == [DOMESTIC[0], DOMESTIC[2]]
    assert results[1].outcome is ProviderOutcome.REJECTED
    assert [results[0].provider_message_id, results[2].provider_message_id] == ["1", "3"]


async def test_mixed_texts_are_never_sent_in_one_request() -> None:
    adapter, rec = adapter_for(respond(201, batch_body(["1", "2", "3"])))
    requests = batch()
    requests[2] = req(text="Another text", recipient=NUMBERS[2])
    results = await adapter.send_batch(requests)
    assert rec.requests == []
    assert {r.outcome for r in results} == {ProviderOutcome.NOT_SENT}
    assert not any(r.retryable for r in results)


async def test_secret_text_and_numbers_never_appear_in_results() -> None:
    adapter, _ = adapter_for(respond(201, batch_body(["1", "2", "3"])))
    for result in await adapter.send_batch(batch()):
        dumped = result.model_dump_json()
        for needle in (SECRET, TEXT, *DOMESTIC, *NUMBERS):
            assert needle not in dumped
