"""The opt-in Frappe ``send_sms`` hook, against the same contract-shaped fake OAN API.

Also checks the hook through Frappe's own ``_send_sms`` (the path Grievance and Auth
use), and the fail-closed behaviour Grievance's ``_send_sms_row`` relies on.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

import frappe
import httpx

from oan_messaging_connector import outbox, sms_hook
from oan_messaging_connector.tests.test_connector import PHONE, FakeOan

HOOK = "oan_messaging_connector.sms_hook.send_sms"
TEXT = "Test: your grievance GRV-1 was received."
OTP_TEXT = "123456 is your password reset code. It expires in 10 minutes."


def hooks_with(*methods):
	"""Patch frappe.get_hooks so ``send_sms`` returns the given hook methods."""
	real = frappe.get_hooks

	def fake(hook=None, *args, **kwargs):
		if hook == "send_sms":
			return list(methods)
		return real(hook, *args, **kwargs)

	return mock.patch.object(frappe, "get_hooks", side_effect=fake)


def grievance_send_sms_row(mobile, message):
	"""The delivery logic of Grievance's ``_send_sms_row`` (develop 4d03412), verbatim
	in substance: fail closed without a hook; trust the hook (an exception) with one."""
	from frappe.core.doctype.sms_settings.sms_settings import _send_sms

	hooked = bool(frappe.get_hooks("send_sms"))
	if not hooked and not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
		raise ValueError("No SMS gateway configured in SMS Settings; message not sent.")
	before = frappe.db.get_value("SMS Log", {}, "name", order_by="creation desc")
	_send_sms([mobile], message, success_msg=False)
	after = frappe.db.get_value("SMS Log", {}, "name", order_by="creation desc")
	delivered = bool(after and after != before)
	if not hooked and not delivered:
		raise ValueError("SMS gateway did not confirm delivery: no SMS Log was written.")
	return {"sms_log": after} if delivered else {}


class TestSmsHook(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		keys = (
			"oan_messaging_enabled",
			"oan_messaging_base_url",
			"oan_messaging_token",
			"oan_messaging_hook_purpose",
			"oan_messaging_hook_expiry_minutes",
		)
		self._conf = {k: frappe.conf.get(k) for k in keys}
		frappe.conf.oan_messaging_enabled = 1
		frappe.conf.oan_messaging_base_url = "https://oan-messaging.example.invalid"
		frappe.conf.oan_messaging_token = "oanmsg_" + "t" * 43
		frappe.conf.oan_messaging_hook_purpose = None
		frappe.conf.oan_messaging_hook_expiry_minutes = None
		settings = frappe.get_single("OAN Messaging Settings")
		settings.max_attempts = 2
		settings.hook_purpose = "transactional"
		settings.hook_expiry_minutes = 30
		settings.save(ignore_permissions=True)
		self.fake = FakeOan()
		outbox._transport = httpx.MockTransport(self.fake)
		self._gateway = frappe.db.get_single_value("SMS Settings", "sms_gateway_url")
		frappe.db.set_single_value("SMS Settings", "sms_gateway_url", "")
		self._has_log = bool(frappe.db.exists("DocType", "SMS Log"))
		self._logs_before = set(frappe.get_all("SMS Log", pluck="name")) if self._has_log else set()

	def tearDown(self):
		outbox._transport = None
		for k, v in self._conf.items():
			frappe.conf[k] = v
		frappe.db.set_single_value("SMS Settings", "sms_gateway_url", self._gateway or "")
		if self._has_log:
			for name in set(frappe.get_all("SMS Log", pluck="name")) - self._logs_before:
				frappe.delete_doc("SMS Log", name, force=True, ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- dispatch commits per row, so cleanup must commit too

	def _new_logs(self):
		if not self._has_log:
			return []
		names = set(frappe.get_all("SMS Log", pluck="name")) - self._logs_before
		return [frappe.get_doc("SMS Log", n) for n in names]

	def _bodies(self):
		return [json.loads(r.content) for r in self.fake.requests if r.method == "POST"]

	# ------------------------------------------------------------------ the hook itself

	def test_accepted_sends_once_and_logs_without_text(self):
		sms_hook.send_sms([PHONE], TEXT, "", False)
		bodies = self._bodies()
		self.assertEqual(len(bodies), 1)
		self.assertEqual(bodies[0]["recipient"], PHONE)
		self.assertEqual(bodies[0]["text"], TEXT)
		self.assertEqual(bodies[0]["purpose"], "transactional")
		self.assertTrue(bodies[0]["source_reference"].startswith("frappe:send_sms:"))
		expires = datetime.fromisoformat(bodies[0]["expires_at"])
		self.assertLess(abs(expires - datetime.now(UTC) - timedelta(minutes=30)), timedelta(minutes=1))
		if self._has_log:
			(log,) = self._new_logs()
			self.assertNotIn(TEXT, log.message)
			self.assertNotIn(PHONE, (log.sent_to or "") + (log.requested_numbers or ""))
			self.assertEqual(log.sent_to, "********01")

	def test_disabled_connector_raises_and_sends_nothing(self):
		frappe.conf.oan_messaging_enabled = 0
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([PHONE], TEXT, "", False)
		self.assertEqual(self.fake.requests, [])
		self.assertEqual(self._new_logs(), [])

	def test_refusal_raises_without_number_or_text(self):
		self.fake.post = [422]
		with self.assertRaises(sms_hook.OanSmsNotSent) as caught:
			sms_hook.send_sms([PHONE], TEXT, "", False)
		self.assertNotIn(PHONE, str(caught.exception))
		self.assertNotIn(TEXT, str(caught.exception))
		self.assertEqual(self._new_logs(), [])

	def test_uncertain_retries_same_key_then_raises(self):
		self.fake.post = [503, 503]
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([PHONE], TEXT, "", False)
		keys = {r.headers["Idempotency-Key"] for r in self.fake.requests}
		self.assertEqual(len(self.fake.requests), 2)
		self.assertEqual(len(keys), 1)

	def test_uncertain_then_accepted_is_success(self):
		self.fake.post = [503, 202]
		sms_hook.send_sms([PHONE], TEXT, "", False)
		self.assertEqual(len({r.headers["Idempotency-Key"] for r in self.fake.requests}), 1)

	def test_text_longer_than_one_part_raises_before_sending(self):
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([PHONE], "x" * 161, "", False)
		self.assertEqual(self.fake.requests, [])

	def test_receiver_list_forms(self):
		sms_hook.send_sms(json.dumps([PHONE]), TEXT, "", False)
		sms_hook.send_sms(PHONE, TEXT, "", False)
		self.assertEqual([b["recipient"] for b in self._bodies()], [PHONE, PHONE])
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([], TEXT, "", False)

	def test_each_recipient_gets_its_own_key(self):
		sms_hook.send_sms([PHONE, "0900000702"], TEXT, "", False)
		self.assertEqual(len({r.headers["Idempotency-Key"] for r in self.fake.requests}), 2)

	def test_otp_purpose_and_ceiling(self):
		frappe.conf.oan_messaging_hook_purpose = "otp"
		frappe.conf.oan_messaging_hook_expiry_minutes = 10
		sms_hook.send_sms([PHONE], OTP_TEXT, "", False)
		self.assertEqual(self._bodies()[0]["purpose"], "otp")
		frappe.conf.oan_messaging_hook_expiry_minutes = 31
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([PHONE], OTP_TEXT, "", False)

	def test_otp_never_in_logs(self):
		frappe.conf.oan_messaging_hook_purpose = "otp"
		frappe.conf.oan_messaging_hook_expiry_minutes = 10
		self.fake.post = [422]
		before = set(frappe.get_all("Error Log", pluck="name"))
		with self.assertRaises(sms_hook.OanSmsNotSent):
			sms_hook.send_sms([PHONE], OTP_TEXT, "", False)
		for name in set(frappe.get_all("Error Log", pluck="name")) - before:
			error = frappe.get_doc("Error Log", name).error or ""
			self.assertNotIn("123456", error)
			self.assertNotIn(PHONE, error)

	def test_settings_require_https(self):
		settings = frappe.get_single("OAN Messaging Settings")
		settings.enabled = 1
		settings.base_url = "http://oan-messaging.example.invalid"
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_settings_reject_otp_expiry_over_30(self):
		settings = frappe.get_single("OAN Messaging Settings")
		settings.hook_purpose = "otp"
		settings.hook_expiry_minutes = 45
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	# --------------------------------------------- through Frappe's own sender

	def test_frappe_send_sms_routes_to_hook(self):
		from frappe.core.doctype.sms_settings.sms_settings import _send_sms

		with hooks_with(HOOK):
			_send_sms([PHONE], TEXT, success_msg=False)  # Grievance's call
			_send_sms(receiver_list=[PHONE], msg=TEXT, success_msg=False)  # Auth's call
		self.assertEqual(len(self._bodies()), 2)

	def test_grievance_row_without_hook_still_fails_closed(self):
		with hooks_with(), self.assertRaises(ValueError):
			grievance_send_sms_row(PHONE, TEXT)
		self.assertEqual(self.fake.requests, [])

	def test_grievance_row_with_hook_accepted(self):
		with hooks_with(HOOK):
			result = grievance_send_sms_row(PHONE, TEXT)
		self.assertEqual(len(self._bodies()), 1)
		if self._has_log:
			self.assertIn("sms_log", result)

	def test_grievance_row_with_hook_fails_closed_on_refusal(self):
		self.fake.post = [422]
		with hooks_with(HOOK), self.assertRaises(sms_hook.OanSmsNotSent):
			grievance_send_sms_row(PHONE, TEXT)

	def test_grievance_row_with_hook_fails_closed_when_disabled(self):
		frappe.conf.oan_messaging_enabled = 0
		with hooks_with(HOOK), self.assertRaises(sms_hook.OanSmsNotSent):
			grievance_send_sms_row(PHONE, TEXT)
		self.assertEqual(self.fake.requests, [])

	def test_auth_gateway_check_sees_the_hook(self):
		try:
			from oan_auth_service.api.v1 import auth
		except ImportError:
			self.skipTest("oan_auth_service is not installed on this bench")
		with hooks_with():
			self.assertFalse(auth._sms_gateway_configured())
		with hooks_with(HOOK):
			self.assertTrue(auth._sms_gateway_configured())
