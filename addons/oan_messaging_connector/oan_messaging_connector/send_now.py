"""Send one SMS now through OAN Messaging, without the outbox.

For messages that must never be stored, such as OTPs: the caller keeps the text only
in its own job arguments and sends from a job that runs after its transaction commits.
Everything else should use ``oan_messaging_connector.api.queue_sms``.

    from oan_messaging_connector.send_now import NotConfigured, OanRejected, OanUncertain, send_sms_now

    submission = send_sms_now(
        idempotency_key=delivery_id,  # a UUID4 created when the code was issued
        recipient=phone,
        text=text,
        source_reference=f"auth:password-reset:{reset_request_id}",
        purpose="otp",
        expires_at=otp_expires_at,  # timezone-aware; at most 30 minutes ahead for otp
        first_submitted_at=issued_at,  # timezone-aware; when the code was issued
    )

Uses the site's OAN Messaging Settings (or ``site_config.json``). The OAN client retries
an uncertain outcome with the same key up to **HTTP Attempts**. Errors:

* ``NotConfigured``: the connector is disabled or not configured on this site.
* ``ValueError``: the message itself is invalid (recipient, key, reference, purpose,
  expiry, length).
* ``OanRejected`` (and ``OanConflict``): OAN refused it. Never retry.
* ``OanUncertain`` (and ``OanRetryDeferred``): OAN may already hold it. Retry later only
  with exactly the same arguments, or give up.
* ``ReconciliationRequired``, ``OanProtocolError``: stop; an operator looks it up.

No error message contains the phone number, the text or the token.
"""

from __future__ import annotations

from datetime import datetime

from .outbox import _run, enabled
from .sms import clean_mobile, fits_one_sms
from .vendor.oan_client import (
	OanClientError,
	OanConflict,
	OanProtocolError,
	OanRejected,
	OanRetryDeferred,
	OanUncertain,
	ReconciliationRequired,
	SmsDelivery,
	Submission,
)

__all__ = [
	"NotConfigured",
	"OanClientError",
	"OanConflict",
	"OanProtocolError",
	"OanRejected",
	"OanRetryDeferred",
	"OanUncertain",
	"ReconciliationRequired",
	"Submission",
	"is_configured",
	"send_sms_now",
]


class NotConfigured(Exception):
	"""OAN Messaging is disabled or not configured on this site."""


def is_configured() -> bool:
	"""Whether this site can send through OAN Messaging (enabled, base URL and token)."""
	return enabled()


def send_sms_now(
	*,
	idempotency_key: str,
	recipient: str,
	text: str,
	source_reference: str,
	purpose: str,
	expires_at: datetime | None,
	first_submitted_at: datetime,
) -> Submission:
	"""Submit one SMS to OAN now. Returns OAN's acceptance (not delivery)."""
	if not enabled():
		raise NotConfigured("OAN Messaging is not enabled or not configured on this site")
	if not text or not fits_one_sms(text):
		raise ValueError("text is empty or does not fit one SMS part")
	phone = clean_mobile(recipient)
	if phone is None:
		raise ValueError("recipient is not an Ethiopian mobile number (09/07 series)")
	delivery = SmsDelivery(
		idempotency_key=idempotency_key,
		recipient=phone,
		text=text,
		source_reference=source_reference,
		purpose=purpose,
		expires_at=expires_at,
		first_submitted_at=first_submitted_at,
	)
	return _run(lambda client: client.submit(delivery))
