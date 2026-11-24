"""OAN Messaging Connector: queue_sms, dispatch and polling against a contract-shaped fake
OAN API (httpx MockTransport, no network). The end-to-end run against a real OAN stack
is recorded in the integration-trial evidence."""

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import frappe
import httpx

from oan_messaging_connector import api, outbox
from oan_messaging_connector.sms import sms_parts

MID = "0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10"
PHONE = "0900000701"
REF = "TEST-CONNECTOR-1"


class FakeOan:
	"""Scripted OAN API: ``post`` lists HTTP codes for successive POSTs."""

	def __init__(self, post=(202,), status="sent", final=False):
		self.post = list(post)
		self.status, self.final = status, final
		self.requests = []

	def __call__(self, request: httpx.Request) -> httpx.Response:
		self.requests.append(request)
		if request.method == "GET":
			return httpx.Response(
				200,
				json={
					"id": MID,
					"idempotency_key": "k",
					"channel": "sms",
					"purpose": "transactional",
					"source_reference": "x:y",
					"status": self.status,
					"final": self.final,
					"recipient_masked": "+2519******01",
					"delivery_receipts_supported": True,
					"attempts": 1,
					"created_at": "2026-09-01T05:12:13Z",
					"updated_at": "2026-09-01T05:12:14Z",
				},
			)
		body = json.loads(request.content)
		code = self.post.pop(0) if self.post else 200
		if code in (202, 200):
			return httpx.Response(
				code,
				json={
					"id": MID,
					"idempotency_key": request.headers["Idempotency-Key"],
					"channel": "sms",
					"status": "accepted",
					"source_reference": body["source_reference"],
					"created_at": "2026-09-01T05:12:13Z",
					"expires_at": body.get("expires_at"),
				},
			)
		return httpx.Response(
			code,
			json={
				"title": "x",
				"status": code,
				"errors": [],
				"code": "validation_failed" if code == 422 else "service_unavailable",
			},
			headers={"content-type": "application/problem+json"},
		)


def queue(**kw):
	base = {
		"recipient": PHONE,
		"text": "Test: your request REF-1 was received.",
		"purpose": "transactional",
		"source_reference": f"test:{REF}:received",
		"reference_doctype": "User",
		"reference_name": REF,
		"event": "received",
	}
	base.update(kw)
	return api.queue_sms(**base)


class TestConnector(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._conf = {
			k: frappe.conf.get(k)
			for k in ("oan_messaging_enabled", "oan_messaging_base_url", "oan_messaging_token")
		}
		frappe.conf.oan_messaging_enabled = 1
		frappe.conf.oan_messaging_base_url = "https://oan-messaging.example.invalid"
		frappe.conf.oan_messaging_token = "oanmsg_" + "t" * 43
		settings = frappe.get_single("OAN Messaging Settings")
		settings.max_attempts = 1
		settings.save(ignore_permissions=True)
		self.fake = FakeOan()
		outbox._transport = httpx.MockTransport(self.fake)
		self._clear()

	def tearDown(self):
		outbox._transport = None
		for k, v in self._conf.items():
			frappe.conf[k] = v
		self._clear()
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- dispatch commits per row, so cleanup must commit too

	def _clear(self):
		# Rows refuse deletion through the document API (audit trail); tests clear their own.
		frappe.db.delete("OAN Message", {"reference_name": ("like", "TEST-CONNECTOR-%")})

	def doc(self, name):
		return frappe.get_doc("OAN Message", name)

	def test_disabled_connector_queues_nothing(self):
		frappe.conf.oan_messaging_enabled = 0
		self.assertIsNone(queue())
		self.assertEqual(outbox.dispatch_queued(), 0)

	def test_queue_then_dispatch_submits_with_the_stored_key(self):
		name = queue()
		row = self.doc(name)
		self.assertEqual(uuid.UUID(row.delivery_id).version, 4)
		self.assertEqual((row.desk_status, row.state), ("Queued", "queued"))
		self.assertEqual(outbox.dispatch_queued(), 1)
		row = self.doc(name)
		self.assertEqual((row.desk_status, row.state, row.oan_message_id), ("Submitted", "accepted", MID))
		post = self.fake.requests[0]
		self.assertEqual(post.headers["Idempotency-Key"], row.delivery_id)
		body = json.loads(post.content)
		self.assertEqual((body["recipient"], body["purpose"]), (PHONE, "transactional"))

	def test_recipient_must_be_an_ethiopian_mobile(self):
		for bad in ("12345", "+254712345678", "0812345678", "09000007O1"):
			self.assertIsNone(queue(recipient=bad), bad)
		name = queue(recipient="0900-000 701")
		self.assertEqual(self.doc(name).recipient, PHONE)  # separators removed
		for good in ("+251900000702", "251700000703", "0700000704"):
			self.assertIsNotNone(queue(recipient=good), good)

	def test_dispatch_and_poll_claim_rows_with_skip_locked(self):
		"""Several workers may run the sweep at once: each claim locks one row and skips
		rows another worker holds."""
		queue()
		with mock.patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql:
			outbox.dispatch_queued()
			outbox.poll_statuses()
		claims = [str(c.args[0]) for c in sql.call_args_list if "FOR UPDATE" in str(c.args[0]).upper()]
		self.assertGreaterEqual(len(claims), 2)
		for query in claims:
			self.assertIn("SKIP LOCKED", query.upper())
			self.assertIn("LIMIT 1", query.upper())

	def test_uncertain_outcome_retries_later_with_the_same_key(self):
		self.fake.post = [503, 202]
		name = queue()
		outbox.dispatch_queued()
		row = self.doc(name)
		self.assertEqual((row.state, row.attempts, row.desk_status), ("queued", 1, "Queued"))
		self.assertTrue(row.next_attempt_at)
		self.assertEqual(outbox.dispatch_queued(), 0)  # not due yet
		frappe.db.set_value("OAN Message", name, "next_attempt_at", None)
		outbox.dispatch_queued()
		self.assertEqual(self.doc(name).state, "accepted")
		self.assertEqual({r.headers["Idempotency-Key"] for r in self.fake.requests}, {row.delivery_id})

	def test_rejection_fails_visibly_and_is_never_retried(self):
		self.fake.post = [422]
		name = queue()
		outbox.dispatch_queued()
		row = self.doc(name)
		self.assertEqual((row.desk_status, row.needs_attention), ("Failed", 1))
		self.assertEqual(outbox.dispatch_queued(), 0)

	def test_poll_records_delivery_and_stops(self):
		self.fake.status, self.fake.final = "delivered", True
		name = queue()
		outbox.dispatch_queued()
		frappe.db.set_value("OAN Message", name, "next_poll_at", None)
		self.assertEqual(outbox.poll_statuses(), 1)
		row = self.doc(name)
		self.assertEqual((row.oan_status, row.oan_final, row.poll_complete), ("delivered", 1, 1))
		self.assertEqual(outbox.poll_statuses(), 0)
		self.assertEqual([r.method for r in self.fake.requests], ["POST", "GET"])

	def test_unknown_outcome_is_flagged_and_never_resubmitted(self):
		self.fake.status = "unknown"
		name = queue()
		outbox.dispatch_queued()
		frappe.db.set_value("OAN Message", name, "next_poll_at", None)
		outbox.poll_statuses()
		row = self.doc(name)
		self.assertEqual((row.oan_status, row.needs_attention, row.poll_complete), ("unknown", 1, 1))
		self.assertEqual([r.method for r in self.fake.requests], ["POST", "GET"])

	def test_broadcast_and_multipart_texts_are_queued(self):
		self.assertIsNotNone(queue(purpose="broadcast", text="x" * 459, max_parts=3))
		self.assertIsNotNone(queue(purpose="alert", text="ቅ" * 201, max_parts=3))
		self.assertIsNotNone(queue(purpose="broadcast", text="x" * 160))  # one part by default

	def test_one_sms_part_unless_the_caller_asks_for_more(self):
		self.assertIsNone(queue(text="x" * 161))  # every module notice: one part
		self.assertIsNone(queue(text="ቅ" * 71))
		self.assertIsNotNone(queue(text="x" * 306, max_parts=2))
		self.assertIsNone(queue(text="x" * 307, max_parts=2))
		self.assertIsNone(queue(text="x" * 10, max_parts=4))  # above OAN's limit

	def test_part_count_matches_oan(self):
		cases = {
			"": 1,
			"a" * 160: 1,
			"a" * 161: 2,
			"a" * 307: 3,
			"ቅ" * 70: 1,
			"ቅ" * 71: 2,
			"ቅ" * 201: 3,
			"{" * 228 + "a": 3,
			"{" * 228 + "aa": 4,
			"😀" * 100: 4,
		}
		for text, parts in cases.items():
			self.assertEqual(sms_parts(text), parts, text[:5])

	def test_invalid_messages_are_refused_without_raising(self):
		self.assertIsNone(queue(text="x" * 460, max_parts=3))  # 3 parts x 153
		self.assertIsNone(queue(text="ቅሬታ " * 51, max_parts=3))  # Amharic: 3 x 67 = 201 at most
		self.assertIsNone(queue(purpose="marketing"))
		otp_exp = datetime.now(UTC) + timedelta(minutes=5)
		self.assertIsNone(queue(purpose="otp", expires_at=otp_exp))  # OTPs are never stored
		self.assertIsNone(queue(recipient=""))
		self.assertIsNone(queue(source_reference="bad reference"))
		past = datetime.now(UTC) - timedelta(minutes=1)
		self.assertIsNone(queue(purpose="alert", expires_at=past))

	def test_message_keeps_its_own_deadline(self):
		exp = datetime.now(UTC) + timedelta(hours=5)
		name = queue(purpose="alert", expires_at=exp, text="Visit V-1 is today at 14:00.")
		outbox.dispatch_queued()
		body = json.loads(self.fake.requests[0].content)
		self.assertEqual(datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")), exp)
		self.assertEqual(self.doc(name).purpose, "alert")

	def test_otp_is_never_stored(self):
		before = frappe.db.count("OAN Message")
		exp = datetime.now(UTC) + timedelta(minutes=5)
		self.assertIsNone(queue(purpose="otp", expires_at=exp, text="Code 123456."))
		self.assertEqual(frappe.db.count("OAN Message"), before)

	def test_settings_failure_does_not_raise(self):
		original = api.enabled
		api.enabled = lambda: (_ for _ in ()).throw(RuntimeError("cache down"))
		try:
			self.assertIsNone(queue())
		finally:
			api.enabled = original

	def test_enqueue_failure_keeps_the_row_for_the_sweep(self):
		original = frappe.enqueue
		frappe.enqueue = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down"))
		try:
			name = queue()
		finally:
			frappe.enqueue = original
		self.assertTrue(name)
		self.assertEqual(self.doc(name).state, "queued")
		self.assertEqual(outbox.dispatch_queued(), 1)

	def test_purge_clears_content_of_finished_rows_only(self):
		self.fake.status, self.fake.final = "delivered", True
		done = queue()
		outbox.dispatch_queued()
		frappe.db.set_value("OAN Message", done, "next_poll_at", None)
		outbox.poll_statuses()
		waiting = queue(reference_name="TEST-CONNECTOR-2")  # still queued
		recent = queue(reference_name="TEST-CONNECTOR-3")
		outbox.dispatch_queued()  # recent and waiting become submitted, polling open
		frappe.db.set_value("OAN Message", waiting, {"state": "queued"})
		old = datetime.now() - timedelta(days=30)
		for n in (done, waiting, recent):
			frappe.db.sql("update `tabOAN Message` set modified = %s where name = %s", (old, n))
		self.assertEqual(outbox.purge_content(), 1)
		row = self.doc(done)
		self.assertEqual((row.text, row.recipient), ("", "********01"))
		self.assertEqual((row.oan_status, row.delivery_id != ""), ("delivered", True))
		self.assertTrue(self.doc(waiting).text)  # queued: kept for sending
		self.assertTrue(self.doc(recent).text)  # submitted, still polling: kept
		self.assertEqual(outbox.purge_content(), 0)  # idempotent

	def test_recent_finished_rows_keep_content(self):
		self.fake.status, self.fake.final = "delivered", True
		name = queue()
		outbox.dispatch_queued()
		frappe.db.set_value("OAN Message", name, "next_poll_at", None)
		outbox.poll_statuses()
		self.assertEqual(outbox.purge_content(), 0)
		self.assertTrue(self.doc(name).text)

	def test_messages_for_lists_delivery_state(self):
		queue()
		rows = api.messages_for("User", REF)
		self.assertEqual([r.desk_status for r in rows], ["Queued"])

	def test_rows_cannot_be_deleted(self):
		name = queue()
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("OAN Message", name, ignore_permissions=True)

	def test_queue_never_raises_into_the_caller(self):
		# A reference that does not exist (yet) is accepted: the link is informational.
		self.assertTrue(queue(reference_doctype="User", reference_name="TEST-CONNECTOR-MISSING"))
		# A storage failure is logged and rolled back to a savepoint, never raised.
		original = frappe.get_doc

		def broken(*a, **k):
			doc = original(*a, **k)
			if doc.doctype == "OAN Message":  # Error Log inserts must still work
				doc.insert = lambda **kw: (_ for _ in ()).throw(RuntimeError("synthetic"))
			return doc

		frappe.get_doc = broken
		try:
			self.assertIsNone(queue())
		finally:
			frappe.get_doc = original
