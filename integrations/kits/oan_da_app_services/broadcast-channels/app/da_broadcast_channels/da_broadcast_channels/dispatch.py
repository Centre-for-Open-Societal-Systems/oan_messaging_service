"""SMS and Telegram delivery of a DA broadcast (Channels Specification §6.8).

SMS and Telegram are sent independently (since 24 November 2026; this replaces the
Telegram-first rule of §6.8.3). Neither channel waits for the other, and a farmer or agent on
both may get both copies.

create()  records the broadcast and its DA Broadcast Dispatch in the request and queues one
          background job per ticked channel after commit. In-app to Development Agents is the
          DA backend's own services.notifications.broadcast(), called unchanged.
run()     resumable background job for one channel (``part``), re-entered by the one-minute
          scheduler until that channel is finished:
          sms       SMS copy through oan_messaging_connector.queue_sms, at once, one page of
                    recipients per transaction (the page's OAN Message rows and the saved
                    cursor commit together, so a restart never queues a page twice): every
                    farmer of the area from the beneficiary API, or every agent of the area
                    minus the sender. Then the retry list: a recipient whose SMS could not be
                    queued is kept on the dispatch (Farmer ID or DA ID, language, row number;
                    never the phone) and tried again with a fresh phone lookup, waiting longer
                    each time, up to ``sms_retry_attempts`` tries.
          telegram  POST /broadcasts to Bot Services (once; Idempotency-Key), then its progress.
                    With SMS ticked, a Telegram failure is recorded on the dispatch
                    (``telegram_error``; ``telegram_failed`` on a definite refusal) and retried
                    on its own backoff; it never touches the SMS copy.
          The dispatch is Done when every ticked channel is finished. After an upstream error
          the scheduler waits before the next try (backoff).
status()  what GET /broadcasts/<id> returns: per-channel counts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from da_broadcast_channels import rules
from da_broadcast_channels.clients import beneficiary, bot_services
from da_broadcast_channels.clients.http import UpstreamError
from da_services.services import constants as C
from da_services.services import notifications as notif
from da_services.utils import audit as audit_log
from oan_messaging_connector.api import queue_sms
from oan_messaging_connector.sms import MAX_PARTS

DOCTYPE = "DA Broadcast Dispatch"
SETTINGS = "DA Broadcast Channel Settings"
OPEN_STATES = ("Queued", "Telegram sending", "SMS sending")
EVENT_QUEUED = "notification.broadcast.channels_queued"
EVENT_DONE = "notification.broadcast.channels_done"
_REF_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


class Conflict(frappe.ValidationError):
	http_status_code = 409


# ---------------------------------------------------------------------------
# create (in the request)
# ---------------------------------------------------------------------------


def create(who: rules.Sender, data: dict) -> dict:
	req = rules.parse(data)
	rules.authorize(who, req)
	if req.client_op_id:
		replay = _replay(req)
		if replay:
			return replay

	if req.recipients == "das" and "in_app" in req.channels:
		# The DA backend's own broadcast: DA Broadcast record, bell notifications, audit.
		out = notif.broadcast(
			title=req.title,
			body=req.body,
			href="/notifications",
			scope=req.scope,
			scope_value=req.scope_value,
			client_op_id=req.client_op_id,
			source=req.source,
		)
		if out["duplicate"]:  # raced a replay that inserted first
			return _replay(req) or _fail_conflict()
		record = _broadcast_name(req.client_op_id, out["broadcast_id"])
		if not ({"sms", "telegram"} & set(req.channels)):
			return {"broadcast_id": out["broadcast_id"], "recipients": out["recipients"], "duplicate": False}
	else:
		record = _insert_broadcast_record(who, req)

	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"broadcast": record,
			"recipients": req.recipients,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"state": "Queued",
			"send_in_app": int("in_app" in req.channels),
			"send_sms": int("sms" in req.channels),
			"send_telegram": int("telegram" in req.channels),
			"source": req.source,
			"sms_purpose": req.sms_purpose if "sms" in req.channels else None,
			"sms_parts": _parts(req) if "sms" in req.channels else 0,
			"expires_at": req.expires_at,
			"sent_by": who.user,
			"sender_role": _sender_role(who),
			"client_op_id": req.client_op_id,
			"payload_digest": req.digest(),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	audit_log.record(
		EVENT_QUEUED,
		entity_type="DA Broadcast",
		entity_name=record,
		details={
			"recipients": req.recipients,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"channels": list(req.channels),
		},
		user=who.user,
	)
	for part in _open_parts(doc):
		enqueue(doc.name, part)
	return status(doc.name) | {"duplicate": False}


def _parts(req: rules.Request) -> int:
	from oan_messaging_connector.sms import sms_parts

	return sms_parts(req.sms_text)


def _sender_role(who: rules.Sender) -> str:
	if who.unrestricted:
		return C.ROLE_ADMIN
	for role in (C.ROLE_COMMS, C.ROLE_SUPERVISOR, C.ROLE_EXECUTIVE, C.ROLE_DA):
		if who.has_any_role(role):
			return role
	return ""


def _broadcast_name(client_op_id: str | None, broadcast_id: str) -> str:
	if client_op_id:
		return frappe.db.get_value("DA Broadcast", {"client_op_id": client_op_id}, "name")
	return broadcast_id


def _insert_broadcast_record(who: rules.Sender, req: rules.Request) -> str:
	"""The DA Broadcast record (history and the unique client_op_id) without bell rows."""
	if req.recipients == "das" and not notif._broadcast_recipients(req.scope, req.scope_value):
		frappe.throw(
			_("No active Development Agent is assigned to {0} {1}; check the scope value.").format(
				req.scope, req.scope_value
			),
			frappe.ValidationError,
		)
	record = frappe.get_doc(
		{
			"doctype": "DA Broadcast",
			"title": req.title,
			"body": req.body,
			"source": req.source,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"sent_by": who.user,
			"sent_at": now_datetime(),
			"client_op_id": req.client_op_id,
		}
	)
	record.flags.ignore_permissions = True
	frappe.db.savepoint("da_broadcast_channels")
	try:
		record.insert()
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point="da_broadcast_channels")
		_fail_conflict()
	return record.name


def _replay(req: rules.Request) -> dict | None:
	"""The original answer for a repeated client_op_id; a different body is a conflict."""
	record = frappe.db.get_value("DA Broadcast", {"client_op_id": req.client_op_id}, "name")
	if not record:
		return None
	row = frappe.db.get_value(DOCTYPE, {"broadcast": record}, ["name", "payload_digest"], as_dict=True)
	if row and row.payload_digest == req.digest():
		return status(row.name) | {"duplicate": True}
	if not row and req.recipients == "das" and req.channels == ("in_app",):
		# An in-app-only replay of the DA backend's own broadcast: its own answer.
		count = frappe.db.get_value("DA Broadcast", record, "recipients")
		return {"broadcast_id": req.client_op_id, "recipients": int(count or 0), "duplicate": True}
	_fail_conflict()


def _fail_conflict():
	frappe.throw(_("client_op_id was already used for a different broadcast."), Conflict)


# ---------------------------------------------------------------------------
# run (background job)
# ---------------------------------------------------------------------------


def enqueue(name: str, part: str) -> None:
	"""One job per channel, so neither channel ever waits for the other."""
	frappe.enqueue(
		"da_broadcast_channels.dispatch.run",
		name=name,
		part=part,
		queue="long",
		job_id=f"da_broadcast_dispatch:{name}:{part}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def _sms_done(doc) -> bool:
	return not doc.send_sms or (doc.sms_cursor == "end" and not json.loads(doc.sms_retry or "[]"))


def _telegram_done(doc) -> bool:
	return not doc.send_telegram or bool(doc.telegram_finished or doc.telegram_failed)


def _open_parts(doc) -> list[str]:
	"""The ticked channels not finished yet, SMS first."""
	return [p for p, done in (("sms", _sms_done(doc)), ("telegram", _telegram_done(doc))) if not done]


def _due(row, part: str, now: datetime) -> bool:
	"""SMS (and Telegram alone) wait on ``next_run_at``; Telegram next to SMS on its own
	``telegram_next_run_at``, so a Telegram outage never holds the SMS back."""
	at = row.telegram_next_run_at if part == "telegram" and row.send_sms else row.next_run_at
	return not at or get_datetime(at) <= now


def resume_open() -> None:
	"""Scheduler (every minute): re-enter every unfinished channel of every open dispatch
	that is due (after an upstream error, or while SMS retries wait, at its next run time)."""
	now = now_datetime()
	rows = frappe.get_all(
		DOCTYPE,
		filters={"state": ("in", OPEN_STATES)},
		fields=[
			"name",
			"send_sms",
			"send_telegram",
			"sms_cursor",
			"sms_retry",
			"next_run_at",
			"telegram_finished",
			"telegram_failed",
			"telegram_next_run_at",
		],
	)
	for row in rows:
		parts = _open_parts(row)
		if not parts:  # every channel finished, the dispatch not yet closed
			enqueue(row.name, "sms" if row.send_sms else "telegram")
			continue
		for part in parts:
			if _due(row, part, now):
				enqueue(row.name, part)


def _commit() -> None:
	"""The job commits per step and per page (tests replace this to keep their transaction)."""
	frappe.db.commit()


def _rollback() -> None:
	frappe.db.rollback()


def _settings():
	return frappe.get_cached_doc(SETTINGS)


def backoff_minutes(tries: int) -> int:
	"""Wait before the next try: the first wait, doubled after every try, at most an hour."""
	first = max(int(_settings().retry_backoff_minutes or 1), 1)
	return min(first * 2 ** max(tries - 1, 0), 60)


def _permanent(exc: UpstreamError) -> bool:
	return exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429)


def run(name: str, part: str | None = None) -> None:
	"""Run one channel (``part``: ``sms`` or ``telegram``), or every ticked channel when no
	part is given (jobs queued by an earlier version; direct calls). SMS goes first, so a
	slow Telegram never delays it."""
	for channel in (part,) if part else ("sms", "telegram"):
		doc = frappe.get_doc(DOCTYPE, name)
		if doc.state not in OPEN_STATES:
			return
		try:
			if channel == "sms" and not _sms_done(doc):
				_sms_step(doc)
				_retry_step(doc)
			elif channel == "telegram" and not _telegram_done(doc):
				_telegram_step(doc)
		except UpstreamError as exc:
			_upstream_failed(name, exc)
	_finish_if_done(name)


def _upstream_failed(name: str, exc: UpstreamError) -> None:
	"""An SMS upstream error (or Telegram when it is the only channel): recorded and tried
	again after the backoff; a definite refusal fails the dispatch."""
	_rollback()
	tries = int(frappe.db.get_value(DOCTYPE, name, "retry_count") or 0) + 1
	frappe.db.set_value(
		DOCTYPE,
		name,
		{
			"error": exc.code[:140],
			"retry_count": tries,
			"next_run_at": add_to_date(now_datetime(), minutes=backoff_minutes(tries)),
		},
	)
	frappe.log_error(f"Broadcast dispatch {name}: {exc}", "DA Broadcast Channels")
	if _permanent(exc):
		_finish_if_open(name, "Failed")
	_commit()


def _telegram_step(doc) -> None:
	"""POST /broadcasts once, then read its progress. Telegram alone keeps the dispatch's own
	error handling (retried, or Failed on a definite refusal). Next to SMS, a failure is
	recorded on the Telegram fields and retried on Telegram's own backoff; a definite
	refusal, or an expired broadcast, marks Telegram failed. The SMS copy is never touched."""
	try:
		bot = bot_services.get_client()
		if not doc.telegram_broadcast_id:
			created = bot.create(_idempotency_key(doc), _telegram_body(doc))
			values = {
				"telegram_broadcast_id": created.broadcast_id,
				"telegram_recipients": created.recipients,
				"not_on_telegram": json.dumps(created.not_on_telegram),
				"telegram_started_at": now_datetime(),
				"telegram_error": None,
				"telegram_next_run_at": None,
			}
			if not doc.send_sms:  # the SMS job owns the state while it runs
				values |= {"state": "Telegram sending", "error": None}
			doc.db_set(values)
			_commit()
		progress = bot.progress(doc.telegram_broadcast_id)
	except UpstreamError as exc:
		if not doc.send_sms:
			raise
		_rollback()
		tries = int(doc.telegram_retry_count or 0) + 1
		values = {"telegram_error": exc.code[:140], "telegram_retry_count": tries}
		if _permanent(exc) or rules.expired(doc.expires_at):
			values |= {"telegram_failed": 1, "telegram_next_run_at": None}
		else:
			values["telegram_next_run_at"] = add_to_date(now_datetime(), minutes=backoff_minutes(tries))
		doc.db_set(values)
		frappe.log_error(f"Broadcast dispatch {doc.name} (Telegram): {exc}", "DA Broadcast Channels")
		_commit()
		return
	doc.db_set(
		{
			"telegram_counts": json.dumps(progress.counts),
			"telegram_finished": int(progress.finished),
			"telegram_error": None,
			"telegram_next_run_at": None,
		}
	)
	_commit()


def _idempotency_key(doc) -> str:
	return doc.client_op_id or f"da-broadcast-{doc.broadcast}"


def _telegram_body(doc) -> dict:
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["title", "body"], as_dict=True)
	body = {
		"recipients": doc.recipients,
		"scope": {"level": doc.scope, "code": doc.scope_value},
		"sender_ref": frappe.db.get_value("User", doc.sent_by, "da_registry_id") or doc.sent_by,
		"sender_role": doc.sender_role,
		"source": doc.source,
		"source_reference": _ref(f"da_broadcast:{doc.broadcast}"),
		"title": record.title,
		"body": record.body or "",
	}
	if doc.expires_at:
		body["expires_at"] = doc.expires_at
	return body


def _sms_step(doc) -> None:
	if doc.state != "SMS sending":
		doc.db_set({"state": "SMS sending", "sms_started_at": now_datetime()})
		_commit()
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["title", "body"], as_dict=True)
	text = (record.body or "").strip() or record.title
	expires = datetime.fromisoformat(doc.expires_at) if doc.expires_at else None
	if doc.recipients == "farmers":
		_sms_farmers(doc, text, expires)
	else:
		_sms_agents(doc, text, expires)


def _sms_farmers(doc, text: str, expires: datetime | None) -> None:
	page_size = int(_settings().page_size or 500)
	client = beneficiary.get_client()
	cursor = doc.sms_cursor or None
	if cursor == "end":
		return
	for page in beneficiary.iter_pages(client, doc.scope, doc.scope_value, cursor, page_size):
		if rules.expired(doc.expires_at):
			doc.db_set({"error": "expired", "sms_cursor": "end"})  # the SMS pass is over
			_commit()
			return
		counts = {"sms_targeted": 0, "sms_queued": 0, "sms_no_phone": 0, "sms_not_queued": 0}
		retry = _retry_list(doc)
		# Row number of each farmer in this broadcast: the rows read before this page plus
		# its place in the page. The page and the counts commit together, so it is the
		# same after a restart.
		row = int(doc.sms_targeted or 0)
		for farmer in page.farmers:
			row += 1
			counts["sms_targeted"] += 1
			if not farmer.phone:
				counts["sms_no_phone"] += 1
			else:
				_queue_one(
					doc, farmer.phone, text, row, farmer.farmer_id, farmer.language, expires, counts, retry
				)
		_save_page(doc, counts, page.next_cursor or "end", retry)


def _sms_agents(doc, text: str, expires: datetime | None) -> None:
	"""Every agent of the area minus the sender, whether or not they are on Telegram."""
	if doc.sms_cursor == "end":
		return
	from da_services.integrations.da_registry.client import get_client as registry

	da_ids = _agent_ids(doc)
	counts = dict.fromkeys(("sms_targeted", "sms_queued", "sms_no_phone", "sms_not_queued"), 0)
	client = registry("service")
	retry = _retry_list(doc)
	for row, da_id in enumerate(da_ids, start=1):
		counts["sms_targeted"] += 1
		ref = client.get_da(da_id)
		phone = getattr(ref, "phone", None) if ref else None
		if not phone:
			counts["sms_no_phone"] += 1
			continue
		_queue_one(doc, phone, text, row, f"da:{da_id}", None, expires, counts, retry)
	_save_page(doc, counts, "end", retry)


def _agent_ids(doc) -> list[str]:
	users = [u for u in notif._broadcast_recipients(doc.scope, doc.scope_value) if u != doc.sent_by]
	if not users:
		return []
	rows = frappe.get_all(
		"DA RBAC Assignment",
		filters={"user": ("in", users), "role": C.ROLE_DA, "active": 1},
		fields=["user", "da_id"],
	)
	ids = {r.da_id for r in rows if r.da_id}
	ids |= {v for v in frappe.get_all("User", filters={"name": ("in", users)}, pluck="da_registry_id") if v}
	return sorted(ids)


def _source_reference(doc, row: int) -> str:
	"""No personal data: the broadcast and the recipient's row number in it."""
	return _ref(f"da_broadcast:{doc.broadcast}:{row}")


def _queue(doc, phone, text, row, language, expires) -> str | None:
	return queue_sms(
		recipient=phone,
		text=text,
		purpose=doc.sms_purpose,
		source_reference=_source_reference(doc, row),
		reference_doctype=DOCTYPE,
		reference_name=doc.name,
		event="broadcast_sms",
		language=language,
		expires_at=expires,
		max_parts=MAX_PARTS,  # broadcasts may be multi-part (SMS-09); module notices stay one
	)


def _queue_one(doc, phone, text, row, who, language, expires, counts, retry) -> None:
	"""Queue one SMS. ``queue_sms`` returns None when nothing was queued (it rolls its row
	back): the recipient goes on the retry list, without the phone."""
	if _queue(doc, phone, text, row, language, expires):
		counts["sms_queued"] += 1
		return
	counts["sms_not_queued"] += 1
	retry.append({"row": row, "who": who, "language": language, "tries": 0, "next": None})


def _retry_list(doc) -> list[dict]:
	return list(json.loads(doc.sms_retry or "[]"))


def _save_page(doc, counts: dict, cursor: str, retry: list[dict]) -> None:
	"""The page's OAN Message rows, the counts, the retry list and the cursor commit together."""
	values = {k: int(doc.get(k) or 0) + v for k, v in counts.items()}
	values["sms_cursor"] = cursor
	values["sms_retry"] = json.dumps(retry)
	doc.db_set(values)
	_commit()


def _phone_again(doc, who: str) -> str | None:
	"""A fresh phone lookup for a retry: the beneficiary API (farmers) or the DA Registry."""
	if who.startswith("da:"):
		from da_services.integrations.da_registry.client import get_client as registry

		ref = registry("service").get_da(who[3:])
		return getattr(ref, "phone", None) if ref else None
	farmer = beneficiary.get_client().farmer(who)
	return farmer.phone if farmer else None


def _already_queued(doc, row: int) -> bool:
	return bool(
		frappe.db.exists(
			"OAN Message",
			{
				"reference_doctype": DOCTYPE,
				"reference_name": doc.name,
				"source_reference": _source_reference(doc, row),
			},
		)
	)


def _retry_step(doc) -> bool:
	"""Try the retry list again. True when it is empty or spent (the SMS pass is over)."""
	retry = _retry_list(doc)
	if not retry:
		return True
	limit = max(int(_settings().sms_retry_attempts or 0), 0)
	now = now_datetime()
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["title", "body"], as_dict=True)
	text = (record.body or "").strip() or record.title
	expires = datetime.fromisoformat(doc.expires_at) if doc.expires_at else None
	queued = not_sent = 0
	keep: list[dict] = []
	for entry in retry:
		if rules.expired(doc.expires_at) or entry["tries"] >= limit:
			not_sent += 1
			continue
		if entry["next"] and get_datetime(entry["next"]) > now:
			keep.append(entry)
			continue
		if _already_queued(doc, entry["row"]):
			queued += 1
			continue
		entry["tries"] += 1
		try:
			phone = _phone_again(doc, entry["who"])
		except UpstreamError:
			phone = None
		if phone and _queue(doc, phone, text, entry["row"], entry["language"], expires):
			queued += 1
		elif entry["tries"] >= limit:
			not_sent += 1
		else:
			entry["next"] = add_to_date(now, minutes=backoff_minutes(entry["tries"])).isoformat()
			keep.append(entry)
	values = {
		"sms_retry": json.dumps(keep),
		"sms_queued": int(doc.sms_queued or 0) + queued,
		"sms_not_sent": int(doc.sms_not_sent or 0) + not_sent,
		"next_run_at": min((get_datetime(e["next"]) for e in keep), default=None),
	}
	doc.db_set(values)
	_commit()
	return not keep


def _finish_if_done(name: str) -> None:
	"""Close the dispatch once every ticked channel is finished. The SMS and Telegram jobs
	may end at the same time: the row lock lets exactly one of them close it."""
	_commit()
	if frappe.db.get_value(DOCTYPE, name, "state", for_update=True) not in OPEN_STATES:
		return
	doc = frappe.get_doc(DOCTYPE, name)
	parts = _open_parts(doc)
	if not parts:
		_finish(doc, "Done")
	elif doc.send_sms and parts == ["telegram"] and doc.state != "Telegram sending":
		doc.db_set("state", "Telegram sending")  # the SMS is over; Telegram still going
	_commit()


def _finish_if_open(name: str, state: str) -> None:
	if frappe.db.get_value(DOCTYPE, name, "state", for_update=True) in OPEN_STATES:
		_finish(frappe.get_doc(DOCTYPE, name), state)


def _finish(doc, state: str) -> None:
	doc.db_set({"state": state, "finished_at": now_datetime(), "next_run_at": None})
	if doc.recipients == "farmers":
		reached = max(int(doc.sms_targeted or 0), int(doc.telegram_recipients or 0))
		frappe.db.set_value("DA Broadcast", doc.broadcast, "recipients", reached, update_modified=False)
	audit_log.record(
		EVENT_DONE,
		entity_type="DA Broadcast",
		entity_name=doc.broadcast,
		details={"state": state, **{k: v for k, v in status(doc.name).items() if k in ("sms", "telegram")}},
		user=doc.sent_by,
	)
	_commit()


def _ref(value: str) -> str:
	return _REF_UNSAFE.sub("_", value)[:128]


# ---------------------------------------------------------------------------
# status (GET /broadcasts/<id>)
# ---------------------------------------------------------------------------


def status(name: str) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["client_op_id", "recipients"], as_dict=True)
	out = {
		"broadcast_id": record.client_op_id or doc.broadcast,
		"recipients": doc.recipients,
		"scope": doc.scope,
		"scope_value": doc.scope_value,
		"state": doc.state,
		"channels": [c for c in ("in_app", "sms", "telegram") if doc.get(f"send_{c}")],
		"error": doc.error,
	}
	if doc.send_in_app:
		out["in_app"] = {"recipients": int(record.recipients or 0)}
	if doc.send_telegram:
		out["telegram"] = {
			"recipients": int(doc.telegram_recipients or 0),
			"not_on_telegram": len(json.loads(doc.not_on_telegram or "[]")),
			"finished": bool(doc.telegram_finished),
			"failed": bool(doc.telegram_failed),
			"error": doc.telegram_error,
			**json.loads(doc.telegram_counts or "{}"),
		}
	if doc.send_sms:
		out["sms"] = {
			"purpose": doc.sms_purpose,
			"parts": int(doc.sms_parts or 0),
			"read": int(doc.sms_targeted or 0),
			"queued": int(doc.sms_queued or 0),
			# Kept for the read contract; always 0 since SMS and Telegram are independent.
			"skipped_on_telegram": int(doc.sms_skipped_on_telegram or 0),
			"no_phone": int(doc.sms_no_phone or 0),
			"not_queued": int(doc.sms_not_queued or 0),
			"retrying": len(json.loads(doc.sms_retry or "[]")),
			"not_sent": int(doc.sms_not_sent or 0),
			"by_status": _sms_by_status(doc.name),
		}
	return out


def _sms_by_status(name: str) -> dict[str, int]:
	"""OAN Message rows of this dispatch, by OAN status (blank while not yet submitted)."""
	rows = frappe.get_all(
		"OAN Message",
		filters={"reference_doctype": DOCTYPE, "reference_name": name},
		fields=["oan_status", {"COUNT": "*", "as": "n"}],
		group_by="oan_status",
	)
	return {(r.oan_status or "queued"): int(r.n) for r in rows}


def find(broadcast_id: str) -> str | None:
	"""The dispatch for a broadcast_id (the client_op_id, or the DA Broadcast name)."""
	record = frappe.db.get_value("DA Broadcast", {"client_op_id": broadcast_id}, "name") or (
		broadcast_id if frappe.db.exists("DA Broadcast", broadcast_id) else None
	)
	return frappe.db.get_value(DOCTYPE, {"broadcast": record}, "name") if record else None


def can_read(who: rules.Sender, name: str) -> bool:
	if who.unrestricted or who.has_any_role(C.ROLE_COMMS):
		return True
	return frappe.db.get_value(DOCTYPE, name, "sent_by") == who.user
