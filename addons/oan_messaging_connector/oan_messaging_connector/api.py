"""Public API for caller apps: queue one SMS through the OAN Messaging Service.

    from oan_messaging_connector.api import queue_sms

    queue_sms(
        recipient=doc.phone_number,
        text=rendered_text,                       # one SMS part unless max_parts says more
        purpose="transactional",                  # transactional | alert | broadcast
        source_reference=f"a2c:{doc.name}:application_submitted",
        reference_doctype=doc.doctype, reference_name=doc.name,
        event="application_submitted",
        expires_at=None,                          # aware datetime; required for otp
    )

Call it inside the business transaction. It inserts one **OAN Message** row (its random
``delivery_id`` is the Idempotency-Key) and schedules dispatch **after commit**, so a
rolled-back business change sends nothing. It never calls OAN itself and never raises
because of messaging: when the connector is disabled or the message is invalid it
returns ``None`` and records why in the Error Log, so a business action is never
blocked by an SMS.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import frappe

from .outbox import DOCTYPE, enabled, record_for_insert
from .sms import MAX_PARTS, clean_mobile, sms_parts
from .vendor.outbox_dispatch import OutboxRow

# OTPs are never queued here: the outbox stores the text, and an OTP must not be stored.
# OTP senders send at once with the OAN client (see the handoff guide "OTP senders").
# broadcast: OAN accepts it once its broadcast_enabled setting is on (SMS-03); until then
# OAN refuses it (purpose_not_permitted) and the row shows the refusal.
PURPOSES = frozenset({"transactional", "alert", "broadcast"})
_SOURCE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class InvalidMessage(ValueError):
	pass


def validate(
	recipient: str,
	text: str,
	purpose: str,
	source_reference: str,
	expires_at: datetime | None,
	max_parts: int = 1,
) -> None:
	if not 1 <= max_parts <= MAX_PARTS:
		raise InvalidMessage(f"max_parts must be 1 to {MAX_PARTS}")
	if not recipient or not recipient.strip():
		raise InvalidMessage("recipient is empty")
	if clean_mobile(recipient) is None:
		raise InvalidMessage("recipient is not an Ethiopian mobile number (09/07 series)")
	if purpose == "otp":
		raise InvalidMessage("otp is not queued through the outbox; send it at once with the OAN client")
	if purpose not in PURPOSES:
		raise InvalidMessage(f"purpose must be one of {sorted(PURPOSES)}")
	if not text or sms_parts(text) > max_parts:
		raise InvalidMessage(f"text is empty or longer than {max_parts} SMS part(s)")
	if not _SOURCE_REF.fullmatch(source_reference or ""):
		raise InvalidMessage("source_reference must match [A-Za-z0-9._:-]{1,128}")
	if expires_at is not None and expires_at.tzinfo is None:
		raise InvalidMessage("expires_at must be timezone-aware")


def queue_sms(
	*,
	recipient: str | None,
	text: str,
	purpose: str,
	source_reference: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	event: str | None = None,
	language: str | None = None,
	expires_at: datetime | None = None,
	max_parts: int = 1,
) -> str | None:
	"""Queue one SMS; returns the OAN Message name, or None (disabled, invalid or failed).

	``max_parts``: one SMS part by default (every module notice is designed to fit one).
	A caller that sends longer texts on purpose, such as DA broadcasts, passes up to
	``sms.MAX_PARTS`` (3); each part is charged. Never raises: any failure is logged and
	the caller's business action goes on.
	"""
	try:
		return _queue_sms(
			recipient=recipient,
			text=text,
			purpose=purpose,
			source_reference=source_reference,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			event=event,
			language=language,
			expires_at=expires_at,
			max_parts=max_parts,
		)
	except Exception as exc:  # never break the caller's business action
		_log(f"OAN SMS not queued ({reference_doctype} {reference_name} {event}): {type(exc).__name__}")
		return None


def _log(message: str) -> None:
	try:
		frappe.log_error(message, "OAN Messaging")
	except Exception:  # logging must not raise either
		pass


def _queue_sms(
	*,
	recipient,
	text,
	purpose,
	source_reference,
	reference_doctype,
	reference_name,
	event,
	language,
	expires_at,
	max_parts,
) -> str | None:
	if not enabled():
		return None
	try:
		validate(recipient or "", text, purpose, source_reference, expires_at, max_parts)
	except InvalidMessage as exc:
		_log(f"OAN SMS not queued ({reference_doctype} {reference_name} {event}): {exc}")
		return None
	now = datetime.now(UTC)
	if expires_at is not None and expires_at <= now:
		_log(f"OAN SMS not queued ({reference_name} {event}): already expired")
		return None
	row = OutboxRow(
		delivery_id="",  # set by OAN Message.before_insert (UUID4)
		recipient=clean_mobile(recipient),
		text=text,
		source_reference=source_reference,
		purpose=purpose,
		expires_at=expires_at,
		first_submitted_at=now,
	)
	values: dict[str, Any] = record_for_insert(row)
	values.update(
		{
			"doctype": DOCTYPE,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"event": event,
			"language": language,
			"desk_status": "Queued",
		}
	)
	values.pop("delivery_id")
	doc = frappe.get_doc(values)
	# The reference is informational: it may be a record created in this same
	# transaction, or one a caller deletes later. Never fail the message on it.
	doc.flags.ignore_links = True
	savepoint = "oan_queue_sms"
	frappe.db.savepoint(savepoint)
	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:  # never break the caller's business action
		frappe.db.rollback(save_point=savepoint)
		_log(f"OAN SMS not queued ({reference_doctype} {reference_name} {event}): {type(exc).__name__}")
		return None
	try:
		frappe.enqueue(
			"oan_messaging_connector.outbox.dispatch_queued", queue="short", enqueue_after_commit=True
		)
	except Exception as exc:  # the one-minute sweep still dispatches the row
		_log(f"OAN SMS queued but not enqueued ({doc.name}): {type(exc).__name__}")
	return doc.name


def messages_for(reference_doctype: str, reference_name: str) -> list[dict[str, Any]]:
	"""Delivery state of every message about one business record (for UIs and support)."""
	return frappe.get_all(
		DOCTYPE,
		filters={"reference_doctype": reference_doctype, "reference_name": reference_name},
		fields=["name", "event", "desk_status", "oan_status", "needs_attention", "creation"],
		order_by="creation asc",
	)
