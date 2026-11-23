"""SMS and Telegram delivery of a DA broadcast (Channels Specification §6.8).

create()  records the broadcast and its DA Broadcast Dispatch in the request and queues
          run() after commit. In-app to Development Agents is the DA backend's own
          services.notifications.broadcast(), called unchanged.
run()     resumable background job, re-entered by the one-minute scheduler until Done:
          1. Telegram first: POST /broadcasts to Bot Services (once; Idempotency-Key).
          2. Waits until Telegram has finished or ``sms_fallback_wait_minutes`` passed,
             counted from when the dispatch started. The wait is checked first, and a
             Telegram failure never stops the SMS copy: it then goes to everyone.
          3. SMS copy through oan_messaging_connector.queue_sms, one page of recipients
             per transaction (the page's OAN Message rows and the saved cursor commit
             together, so a restart never queues a page twice): farmers from the
             beneficiary API minus the Farmer IDs reached on Telegram; agents not on
             Telegram (or every agent when Telegram is not ticked).
          4. Retry list: a recipient whose SMS could not be queued is kept on the dispatch
             (Farmer ID or DA ID, language, row number; never the phone). After the pass
             each is tried again with a fresh phone lookup, waiting longer each time, up
             to ``sms_retry_attempts`` tries. Done only when the list is empty or spent.
          After an upstream error the scheduler waits before the next try (backoff).
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
			# The SMS fallback clock starts with the dispatch, even if Telegram never answers.
			"telegram_started_at": now_datetime() if "telegram" in req.channels else None,
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
	enqueue(doc.name)
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


def enqueue(name: str) -> None:
	frappe.enqueue(
		"da_broadcast_channels.dispatch.run",
		name=name,
		queue="long",
		job_id=f"da_broadcast_dispatch:{name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def resume_open() -> None:
	"""Scheduler (every minute): re-enter every unfinished dispatch that is due. A dispatch
	that hit an upstream error, or waits for SMS retries, is due again at ``next_run_at``."""
	now = now_datetime()
	rows = frappe.get_all(DOCTYPE, filters={"state": ("in", OPEN_STATES)}, fields=["name", "next_run_at"])
	for row in rows:
		if not row.next_run_at or get_datetime(row.next_run_at) <= now:
			enqueue(row.name)


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


def run(name: str) -> None:
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.state not in OPEN_STATES:
		return
	try:
		if doc.send_telegram and not _telegram_step(doc):
			return  # waiting for Telegram; the scheduler comes back
		if doc.send_sms:
			_sms_step(doc)
			if not _retry_step(doc):
				return  # SMS retries still waiting; the scheduler comes back
		_finish(doc, "Done")
	except UpstreamError as exc:
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
			_finish(frappe.get_doc(DOCTYPE, name), "Failed")
		_commit()


def _telegram_step(doc) -> bool:
	"""True when the SMS copy may start (or Telegram is all there is and it is finished).

	The fallback wait is checked first and counts from the start of the dispatch. With SMS
	ticked, a Telegram failure never stops the SMS copy: a definite refusal marks Telegram
	failed and the SMS goes now; any other failure is tried again next run, and the SMS
	goes anyway once the wait has passed. Telegram alone keeps the dispatch's own error
	handling (retried, or Failed on a definite refusal)."""
	if doc.telegram_failed:
		return True
	if not doc.telegram_started_at:  # dispatches created before this version
		doc.db_set("telegram_started_at", now_datetime())
		_commit()
	wait = int(_settings().sms_fallback_wait_minutes or 15)
	waited = now_datetime() >= add_to_date(get_datetime(doc.telegram_started_at), minutes=wait)
	try:
		bot = bot_services.get_client()
		if not doc.telegram_broadcast_id:
			created = bot.create(_idempotency_key(doc), _telegram_body(doc))
			doc.db_set(
				{
					"state": "Telegram sending",
					"telegram_broadcast_id": created.broadcast_id,
					"telegram_recipients": created.recipients,
					"not_on_telegram": json.dumps(created.not_on_telegram),
					"error": None,
					"telegram_error": None,
				}
			)
			_commit()
		progress = bot.progress(doc.telegram_broadcast_id)
	except UpstreamError as exc:
		if not doc.send_sms:
			raise
		_rollback()
		doc.db_set("telegram_error", exc.code[:140])
		if _permanent(exc):
			doc.db_set("telegram_failed", 1)
			_commit()
			return True
		_commit()
		return waited
	doc.db_set({"telegram_counts": json.dumps(progress.counts), "telegram_finished": int(progress.finished)})
	_commit()
	if progress.finished:
		return True
	if not doc.send_sms:
		return False
	return waited


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


def _reached_on_telegram(doc) -> set[str]:
	"""Farmer IDs Telegram reached. Empty when Telegram failed or never started; if the
	list cannot be read, the SMS waits for it until the fallback wait has passed, then
	goes to everyone (a farmer may get both copies, never neither)."""
	if not doc.send_telegram or doc.telegram_failed or not doc.telegram_broadcast_id:
		return set()
	try:
		return bot_services.get_client().telegram_recipients(doc.telegram_broadcast_id)
	except UpstreamError as exc:
		wait = int(_settings().sms_fallback_wait_minutes or 15)
		started = get_datetime(doc.telegram_started_at) if doc.telegram_started_at else now_datetime()
		if now_datetime() < add_to_date(started, minutes=wait):
			raise
		doc.db_set("telegram_error", exc.code[:140])
		return set()


def _sms_farmers(doc, text: str, expires: datetime | None) -> None:
	if (doc.sms_cursor or None) == "end":
		return
	skip = _reached_on_telegram(doc)
	page_size = int(_settings().page_size or 500)
	client = beneficiary.get_client()
	cursor = doc.sms_cursor or None
	if cursor == "end":
		return
	for page in beneficiary.iter_pages(client, doc.scope, doc.scope_value, cursor, page_size):
		if rules.expired(doc.expires_at):
			doc.db_set("error", "expired")
			_commit()
			return
		counts = {"sms_targeted": 0, "sms_queued": 0, "sms_skipped_on_telegram": 0, "sms_no_phone": 0}
		counts["sms_not_queued"] = 0
		retry = _retry_list(doc)
		# Row number of each farmer in this broadcast: the rows read before this page plus
		# its place in the page. The page and the counts commit together, so it is the
		# same after a restart.
		row = int(doc.sms_targeted or 0)
		for farmer in page.farmers:
			row += 1
			counts["sms_targeted"] += 1
			if farmer.farmer_id in skip:
				counts["sms_skipped_on_telegram"] += 1
			elif not farmer.phone:
				counts["sms_no_phone"] += 1
			else:
				_queue_one(
					doc, farmer.phone, text, row, farmer.farmer_id, farmer.language, expires, counts, retry
				)
		_save_page(doc, counts, page.next_cursor or "end", retry)


def _sms_agents(doc, text: str, expires: datetime | None) -> None:
	"""Agents of the area minus the sender; with Telegram ticked, only those not on it."""
	if doc.sms_cursor == "end":
		return
	from da_services.integrations.da_registry.client import get_client as registry

	# With Telegram ticked and sent, only the agents not on it; if Telegram failed or never
	# started, every agent.
	telegram_sent = doc.send_telegram and doc.telegram_broadcast_id and not doc.telegram_failed
	only = set(json.loads(doc.not_on_telegram or "[]")) if telegram_sent else None
	da_ids = _agent_ids(doc)
	counts = dict.fromkeys(
		("sms_targeted", "sms_queued", "sms_skipped_on_telegram", "sms_no_phone", "sms_not_queued"), 0
	)
	client = registry("service")
	retry = _retry_list(doc)
	for row, da_id in enumerate(da_ids, start=1):
		counts["sms_targeted"] += 1
		if only is not None and da_id not in only:
			counts["sms_skipped_on_telegram"] += 1
			continue
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
			**json.loads(doc.telegram_counts or "{}"),
		}
	if doc.send_sms:
		out["sms"] = {
			"purpose": doc.sms_purpose,
			"parts": int(doc.sms_parts or 0),
			"read": int(doc.sms_targeted or 0),
			"queued": int(doc.sms_queued or 0),
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
