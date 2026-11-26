"""Frappe ``send_sms`` hook: route Frappe's own SMS sender through OAN Messaging.

**Explicit calls are the preferred integration.** New SMS features call
``oan_messaging_connector.api.queue_sms`` (or, for OTPs, send at once with the OAN
client). This hook exists only so that modules which already send through Frappe's
built-in sender (``frappe.core.doctype.sms_settings.sms_settings._send_sms``) - today
Grievance and Auth - can switch to OAN Messaging without rewriting their send path.

It is **opt-in per module**: installing the connector registers nothing. A module turns
it on with one line in its own ``hooks.py``::

    send_sms = "oan_messaging_connector.sms_hook.send_sms"

Frappe then calls this function, with the same arguments, instead of its HTTP gateway.
A ``send_sms`` hook applies to the whole site: every ``_send_sms`` call on that site goes
through OAN once one app registers it.

What the hook does, compared with ``queue_sms``:

* Sends **at once**, inside the caller's request or job. Nothing is stored in the
  connector, so it is also safe for OTP text.
* One random Idempotency-Key per recipient. An uncertain outcome is retried by the OAN
  client with the same key (``max_attempts`` in OAN Messaging Settings). If it is still
  uncertain, the hook raises; a later resend by the caller is a new message.
* **Raises** on every failure (connector disabled, invalid text, OAN refusal or no
  answer), so a caller that treats an exception as "not sent" fails closed. Grievance
  and Auth skip their own "is an SMS gateway configured?" checks once any ``send_sms``
  hook exists, so this is what keeps them honest.
* Success means **accepted by OAN**, not delivered. The caller gets no delivery status.
* Writes an **SMS Log** row (when the doctype exists) with masked numbers and no text,
  so callers that look for one still find it.

Per-site settings (OAN Messaging Settings, overridable in ``site_config.json``):
``hook_purpose`` (``transactional`` or ``otp``) and ``hook_expiry_minutes``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import frappe

from .outbox import SETTINGS, enabled, mask_recipient
from .send_now import OanClientError, send_sms_now
from .sms import fits_one_sms

HOOK_PURPOSES = frozenset({"transactional", "otp"})
#: OAN's ceilings: an OTP may live at most 30 minutes, anything else at most 7 days.
MAX_EXPIRY_MINUTES = {"otp": 30, "transactional": 7 * 24 * 60}
DEFAULT_EXPIRY_MINUTES = 30
SMS_LOG = "SMS Log"
SENDER = "OAN Messaging"


class OanSmsNotSent(Exception):
	"""The SMS was not accepted by OAN Messaging. Never contains a number or the text."""


def send_sms(
	receiver_list: str | list[str],
	msg: str,
	sender_name: str = "",
	success_msg: bool = True,
) -> None:
	"""Frappe ``send_sms`` hook. Same signature as Frappe's ``_send_sms``.

	Raises ``OanSmsNotSent`` unless OAN accepted the message for every recipient.
	"""
	recipients = _recipients(receiver_list)
	text = frappe.safe_decode(msg or "")
	if not enabled():
		raise OanSmsNotSent("OAN Messaging is not enabled or not configured on this site")
	if not text.strip() or not fits_one_sms(text):
		raise OanSmsNotSent("SMS text is empty or does not fit one SMS part")
	purpose, minutes = _hook_policy()

	accepted: list[str] = []
	failures: list[str] = []
	for recipient in recipients:
		now = datetime.now(UTC)
		try:
			send_sms_now(
				idempotency_key=str(uuid.uuid4()),
				recipient=recipient,
				text=text,
				source_reference=f"frappe:send_sms:{uuid.uuid4().hex[:12]}",
				purpose=purpose,
				expires_at=now + timedelta(minutes=minutes),
				first_submitted_at=now,
			)
		except OanClientError as exc:
			# The client's messages never include numbers, text or credentials.
			failures.append(type(exc).__name__)
			_log(f"SMS through the send_sms hook not accepted by OAN: {exc}")
		except Exception as exc:  # configuration or transport: report the type only
			failures.append(type(exc).__name__)
			_log(f"SMS through the send_sms hook not sent: {type(exc).__name__}")
		else:
			accepted.append(recipient)

	if accepted:
		_write_sms_log(sender_name, recipients, accepted)
	if failures:
		raise OanSmsNotSent(
			f"OAN Messaging did not accept {len(failures)} of {len(recipients)} SMS "
			f"({', '.join(sorted(set(failures)))})"
		)
	if success_msg:
		frappe.msgprint(frappe._("SMS accepted for sending"))


def _recipients(receiver_list: str | list[str]) -> list[str]:
	"""Accept what Frappe's sender accepts: a list, a JSON list, or one number."""
	if isinstance(receiver_list, str):
		try:
			parsed = json.loads(receiver_list)
		except ValueError:
			parsed = receiver_list
		receiver_list = parsed if isinstance(parsed, list) else [parsed]
	recipients = [str(r).strip() for r in receiver_list or [] if r is not None and str(r).strip()]
	if not recipients:
		raise OanSmsNotSent("no recipient")
	return recipients


def _hook_policy() -> tuple[str, int]:
	s = frappe.get_cached_doc(SETTINGS)
	purpose = frappe.conf.get("oan_messaging_hook_purpose") or s.get("hook_purpose") or "transactional"
	minutes = frappe.conf.get("oan_messaging_hook_expiry_minutes") or s.get("hook_expiry_minutes")
	if purpose not in HOOK_PURPOSES:
		raise OanSmsNotSent("hook purpose must be transactional or otp")
	try:
		minutes = int(minutes or DEFAULT_EXPIRY_MINUTES)
	except (TypeError, ValueError):
		raise OanSmsNotSent("hook expiry minutes must be a whole number") from None
	if not 1 <= minutes <= MAX_EXPIRY_MINUTES[purpose]:
		raise OanSmsNotSent(
			f"hook expiry minutes must be between 1 and {MAX_EXPIRY_MINUTES[purpose]} for {purpose}"
		)
	return purpose, minutes


def _write_sms_log(sender_name: str, requested: list[str], accepted: list[str]) -> None:
	"""Evidence for callers that look for an SMS Log. No text, masked numbers only."""
	try:
		if not frappe.db.exists("DocType", SMS_LOG):
			return
		frappe.get_doc(
			{
				"doctype": SMS_LOG,
				"sender_name": sender_name or SENDER,
				"sent_on": frappe.utils.nowdate(),
				"message": "[not stored: accepted by OAN Messaging]",
				"no_of_requested_sms": len(requested),
				"requested_numbers": "\n".join(mask_recipient(r) for r in requested),
				"no_of_sent_sms": len(accepted),
				"sent_to": "\n".join(mask_recipient(r) for r in accepted),
			}
		).insert(ignore_permissions=True)
	except Exception as exc:  # the SMS is already accepted; the log is only evidence
		_log(f"SMS Log not written for an SMS accepted by OAN: {type(exc).__name__}")


def _log(message: str) -> None:
	try:
		frappe.log_error(message, "OAN Messaging")
	except Exception:  # logging must not raise
		pass


__all__ = ["OanSmsNotSent", "send_sms"]
