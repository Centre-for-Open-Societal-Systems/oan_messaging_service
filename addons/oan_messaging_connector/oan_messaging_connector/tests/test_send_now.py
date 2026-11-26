"""send_sms_now: one SMS without the outbox (OTP senders), against the fake OAN API."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta

import frappe
import httpx

from oan_messaging_connector import outbox, send_now
from oan_messaging_connector.tests.test_connector import MID, PHONE, FakeOan

OTP_TEXT = "123456 is your password reset code. It expires in 10 minutes."


class TestSendNow(unittest.TestCase):
	def setUp(self):
		keys = ("oan_messaging_enabled", "oan_messaging_base_url", "oan_messaging_token")
		self._conf = {k: frappe.conf.get(k) for k in keys}
		frappe.conf.oan_messaging_enabled = 1
		frappe.conf.oan_messaging_base_url = "https://oan-messaging.example.invalid"
		frappe.conf.oan_messaging_token = "oanmsg_" + "t" * 43
		settings = frappe.get_single("OAN Messaging Settings")
		settings.max_attempts = 2
		settings.save(ignore_permissions=True)
		self.fake = FakeOan()
		outbox._transport = httpx.MockTransport(self.fake)
		self.key = str(uuid.uuid4())
		self.now = datetime.now(UTC)

	def tearDown(self):
		outbox._transport = None
		for k, v in self._conf.items():
			frappe.conf[k] = v

	def send(self, **kw):
		args = {
			"idempotency_key": self.key,
			"recipient": PHONE,
			"text": OTP_TEXT,
			"source_reference": "auth:password-reset:abc123",
			"purpose": "otp",
			"expires_at": self.now + timedelta(minutes=10),
			"first_submitted_at": self.now,
		}
		args.update(kw)
		return send_now.send_sms_now(**args)

	def test_accepted_returns_the_submission_with_the_callers_key(self):
		sub = self.send()
		self.assertEqual((sub.id, sub.idempotency_key), (MID, self.key))
		body = json.loads(self.fake.requests[0].content)
		self.assertEqual((body["purpose"], body["recipient"]), ("otp", PHONE))
		self.assertEqual(self.fake.requests[0].headers["Idempotency-Key"], self.key)

	def test_not_configured(self):
		frappe.conf.oan_messaging_enabled = 0
		self.assertFalse(send_now.is_configured())
		with self.assertRaises(send_now.NotConfigured):
			self.send()
		self.assertEqual(self.fake.requests, [])

	def test_refusal_is_never_retried(self):
		self.fake.post = [422]
		with self.assertRaises(send_now.OanRejected):
			self.send()
		self.assertEqual(len(self.fake.requests), 1)

	def test_uncertain_is_retried_with_the_same_key_then_raised(self):
		self.fake.post = [503, 503]
		with self.assertRaises(send_now.OanUncertain):
			self.send()
		self.assertEqual({r.headers["Idempotency-Key"] for r in self.fake.requests}, {self.key})

	def test_invalid_messages_are_refused_before_sending(self):
		for kw in (
			{"text": "x" * 161},
			{"purpose": "otp", "expires_at": None},
			{"idempotency_key": "short"},
			{"recipient": "+254712345678"},
		):
			with self.assertRaises(ValueError, msg=kw):
				self.send(**kw)
		self.assertEqual(self.fake.requests, [])

	def test_errors_never_contain_the_phone_or_the_text(self):
		self.fake.post = [422]
		with self.assertRaises(send_now.OanRejected) as caught:
			self.send()
		self.assertNotIn(PHONE, str(caught.exception))
		self.assertNotIn("123456", str(caught.exception))
