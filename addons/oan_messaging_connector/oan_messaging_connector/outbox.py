"""Dispatch and status polling for the OAN Message outbox.

* ``dispatch_queued`` sends due queued rows: one row per transaction,
  ``FOR UPDATE SKIP LOCKED`` (safe with several workers), using the tested
  ``outbox_dispatch.dispatch`` logic. A 202/200 marks the row *Submitted* (accepted by
  OAN), never "Sent". An uncertain outcome is retried later with the same key and body.
* ``poll_statuses`` asks OAN for the status of submitted rows on a backoff schedule
  (``outbox_dispatch.reconcile``). It never resends; ``unknown`` sets
  ``needs_attention``. SMS polling stops after ``receipt_wait_days``.

* ``purge_content`` (daily) clears the message text and masks the recipient of rows
  that no longer need them (no longer queued, and polling finished or never needed),
  once they are older than ``content_retention_days`` (default 7). Delivery metadata
  stays as the audit trail.

Configuration: **OAN Messaging Settings**. ``site_config.json`` keys
``oan_messaging_enabled``, ``oan_messaging_base_url``, ``oan_messaging_token`` and
``oan_messaging_ca_bundle`` override it, so a deployment can inject the token from its secret store.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import frappe
from frappe.query_builder.functions import IfNull
from frappe.utils import now_datetime

from .vendor.oan_client import ClientConfig, OanClient, Secret
from .vendor.outbox_dispatch import OutboxRow, dispatch, reconcile

DOCTYPE = "OAN Message"
SETTINGS = "OAN Messaging Settings"
ROW_FIELDS = [f.name for f in dataclasses.fields(OutboxRow)]
DESK_STATUS = {"queued": "Queued", "accepted": "Submitted"}
_DATES = ("expires_at", "first_submitted_at", "next_attempt_at", "next_poll_at", "poll_deadline")
_FLAGS = ("oan_final", "needs_attention", "poll_complete")
#: Tests may inject an httpx transport (e.g. ``httpx.MockTransport``).
_transport: Any = None


def _settings() -> dict[str, Any]:
	s = frappe.get_cached_doc(SETTINGS)
	token = frappe.conf.get("oan_messaging_token")
	if not token and s.get("token"):
		token = s.get_password("token", raise_exception=False)
	return {
		"enabled": bool(frappe.conf.get("oan_messaging_enabled", s.get("enabled"))),
		"base_url": frappe.conf.get("oan_messaging_base_url") or s.get("base_url"),
		"token": token,
		"ca_bundle": frappe.conf.get("oan_messaging_ca_bundle") or s.get("ca_bundle") or None,
		"max_attempts": int(s.get("max_attempts") or 3),
		"receipt_wait": timedelta(days=int(s.get("receipt_wait_days") or 7)),
	}


def enabled() -> bool:
	s = _settings()
	return bool(s["enabled"] and s["base_url"] and s["token"])


def _run(step: Callable[[OanClient], Awaitable[Any]]) -> Any:
	s = _settings()
	cfg = ClientConfig(
		base_url=s["base_url"],
		token=Secret(s["token"]),
		ca_bundle=s["ca_bundle"],
		max_attempts=s["max_attempts"],
	)

	async def go() -> Any:
		client = OanClient(cfg, transport=_transport) if _transport is not None else OanClient(cfg)
		async with client:
			return await step(client)

	return asyncio.run(go())  # Frappe workers are synchronous


def _iso(value: datetime) -> str:
	return value.astimezone(UTC).isoformat()  # always UTC: lexically sortable in SQL


def record_for_insert(row: OutboxRow) -> dict[str, Any]:
	rec = row.to_record()
	for name in _FLAGS:
		rec[name] = 1 if rec[name] else 0
	for name in _DATES:
		if rec[name]:
			rec[name] = _iso(datetime.fromisoformat(rec[name]))
	return rec


def _row(doc: Any) -> OutboxRow:
	rec = {name: doc.get(name) or None for name in ROW_FIELDS}
	rec["attempts"] = int(doc.get("attempts") or 0)
	rec["polls"] = int(doc.get("polls") or 0)
	for name in _FLAGS:
		rec[name] = bool(doc.get(name))
	return OutboxRow.from_record(rec)


def _save(name: str, row: OutboxRow) -> None:
	rec = record_for_insert(row)
	rec.pop("delivery_id")  # never rewritten
	rec["desk_status"] = DESK_STATUS.get(row.state, "Failed")
	frappe.db.set_value(DOCTYPE, name, rec, update_modified=True)


def _retention_days() -> int:
	conf = frappe.conf.get("oan_messaging_content_retention_days")
	if conf is None:
		conf = frappe.db.get_single_value(SETTINGS, "content_retention_days")
	try:
		return max(1, int(conf or 7))
	except (TypeError, ValueError):
		return 7


def mask_recipient(recipient: str | None) -> str:
	"""Keep only the last two digits, e.g. ``********01``."""
	value = (recipient or "").strip()
	return "*" * max(0, len(value) - 2) + value[-2:] if value else ""


def _table():
	return frappe.qb.DocType(DOCTYPE)


def purge_content(limit: int = 500) -> int:
	"""Clear text and mask the recipient of finished rows past the retention period.

	Runs whether or not the connector is enabled (privacy). Queued rows, and submitted
	rows still being polled, keep their content. Returns the number of rows purged.
	The scheduler commits when the job ends.
	"""
	# ``modified`` is stored in the site's time zone, like now_datetime().
	cutoff = now_datetime() - timedelta(days=_retention_days())
	m = _table()
	rows = (
		frappe.qb.from_(m)
		.select(m.name, m.recipient)
		.where(IfNull(m.text, "") != "")
		.where(m.state != "queued")
		.where((m.poll_complete == 1) | (m.state != "accepted"))
		.where(m.modified < cutoff)
		.orderby(m.modified)
		.limit(limit)
	).run(as_dict=True)
	for row in rows:
		frappe.db.set_value(
			DOCTYPE,
			row.name,
			{"text": "", "recipient": mask_recipient(row.recipient)},
			update_modified=False,
		)
	return len(rows)


def _claim(condition, order_by) -> dict[str, Any] | None:
	"""Lock and read one due row, skipping rows another worker holds."""
	m = _table()
	rows = (
		frappe.qb.from_(m)
		.select(m.name, *(m[f] for f in ROW_FIELDS))
		.where(condition)
		.orderby(order_by)
		.limit(1)
		.for_update(skip_locked=True)
	).run(as_dict=True)
	return rows[0] if rows else None


def _commit_row() -> None:
	"""One commit per row: it releases this row's lock and records the outcome of the
	OAN call before the next row, so a crash never makes the next run resend it blind."""
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit


def dispatch_queued(limit: int = 50) -> int:
	"""Send due queued rows; returns how many dispatch runs were made."""
	if not enabled():
		return 0
	m = _table()
	runs = 0
	seen: set[str] = set()
	for _ in range(limit):
		now = datetime.now(UTC)
		due = (m.state == "queued") & (m.next_attempt_at.isnull() | (m.next_attempt_at <= _iso(now)))
		record = _claim(due, m.creation)
		if record is None or record["name"] in seen:  # each row at most once per run
			break
		seen.add(record["name"])
		row = _row(record)
		_run(lambda client, row=row, now=now: dispatch(client, row, now))
		_save(record["name"], row)
		_commit_row()
		runs += 1
	return runs


def poll_statuses(limit: int = 100) -> int:
	"""GET the OAN status of submitted rows whose poll is due; never resends."""
	if not enabled():
		return 0
	now = datetime.now(UTC)
	wait = _settings()["receipt_wait"]
	m = _table()
	due = (
		(m.state == "accepted")
		& (m.poll_complete == 0)
		& (m.oan_final == 0)
		& (m.next_poll_at.isnull() | (m.next_poll_at <= _iso(now)))
	)
	polled = 0
	seen: set[str] = set()
	for _ in range(limit):
		record = _claim(due, m.next_poll_at)
		if record is None or record["name"] in seen:  # each row at most once per run
			break
		seen.add(record["name"])
		row = _row(record)
		_run(lambda client, row=row: reconcile(client, row, now, receipt_wait=wait))
		_save(record["name"], row)
		_commit_row()
		polled += 1
	return polled
