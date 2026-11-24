"""DA broadcast end to end on a DA site (test numbers only, 0900000xxx).

Run from the bench's sites directory, with the kit's stub server next to this file:

    cd frappe-bench/sites && ../env/bin/python <kit>/e2e/e2e_broadcast.py da.trial [--no-oan]

SMS and Telegram are sent independently: neither waits for the other.

1. Writes a stub fixture for the site's first woreda: 6 farmers (two linked on Telegram,
   one of whom fails there, one without a phone) and starts stubs/stub_server.py.
2. Points DA Broadcast Channel Settings at the stub (beneficiary API and Bot Services).
3. Broadcast A: a Communications Officer sends a Weather message (2 SMS parts) to all
   farmers of the woreda on SMS and Telegram. The SMS job queues all 5 farmers with a phone
   before Bot Services is called at all; Telegram runs on its own (1 sent, 1 failed). The
   SMS reach OAN Messaging and the fake 8028 (purpose alert).
4. Broadcast B: an Informational message on SMS only (purpose broadcast), delivered once
   OAN runs with OAN_MSG_BROADCAST_ENABLED (SMS-03 broadcast ageing).
5. Broadcast C: Bot Services is down (the stub answers 503). The SMS still goes at once;
   the Telegram error is recorded on the dispatch. Bot Services comes back; Telegram is
   sent and the broadcast is Done.
6. Replays A's client_op_id: the original answer, nothing sent twice.

--no-oan stops at "SMS queued in the connector outbox": it skips the delivery checks
through OAN Messaging and the fake 8028 (for a bench without that stack).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

import frappe

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
SITE = ARGS[0] if ARGS else "da.trial"
NO_OAN = "--no-oan" in sys.argv
# What the delivery checks prove: OAN Messaging and the fake 8028 delivered the SMS, or with
# --no-oan only that they are queued in the connector outbox.
DELIVERED = (
	"queued in the outbox (--no-oan: delivery not checked)" if NO_OAN else "delivered by the fake 8028"
)
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18200
COMMS = "e2e-bc-comms@da.local"

frappe.init(site=SITE, sites_path=".")
frappe.connect()
frappe.set_user("Administrator")
frappe.flags.in_import = True

from frappe.utils import today

from da_broadcast_channels import api as bc_api
from da_broadcast_channels import dispatch, rules
from da_services.services import constants as C
from da_services.tests.fixtures import live_geo
from oan_messaging_connector import outbox


def check(cond, label):
	print(("PASS " if cond else "FAIL ") + label, flush=True)
	if not cond:
		check.failed = True


check.failed = False
geo = live_geo()

# -- 1. stub fixture and server ------------------------------------------------------
area = {k: geo[k] for k in ("region", "zone", "woreda", "kebele")}


def farmer(n, **kw):
	row = {
		"farmer_id": f"FR-E2E-BC-{n}",
		"first_name": f"Farmer{n}",
		"language_spoken": "am",
		"record_status": "ACTIVE",
		"phones": [{"number": f"090000081{n}", "is_primary": True}],
		**{f"{k}_code": v for k, v in area.items()},
	}
	row.update(kw)
	return row


fixture = {
	"farmers": [
		farmer(1, telegram=True),  # Telegram sent; gets the SMS too (independent channels)
		farmer(2, telegram=True, telegram_fails=True),  # Telegram failed; gets the SMS
		farmer(3),
		farmer(4),
		farmer(5),
		farmer(6, phones=[]),  # no phone: counted, nothing sent
	],
	"agents": [],
}
fixture_path = os.path.join(frappe.get_site_path("private", "files"), "e2e_broadcast_fixture.json")
with open(fixture_path, "w", encoding="utf-8") as fh:
	json.dump(fixture, fh)
stub = subprocess.Popen(
	[
		sys.executable,
		os.path.join(HERE, "..", "stubs", "stub_server.py"),
		"--fixture",
		fixture_path,
		"--port",
		str(PORT),
		"--finish-after",
		"3",
	],
	stdout=subprocess.PIPE,
	text=True,
)
print(stub.stdout.readline().strip(), flush=True)

try:
	# -- 2. settings ---------------------------------------------------------------
	base = f"http://127.0.0.1:{PORT}"
	settings = frappe.get_single("DA Broadcast Channel Settings")
	settings.update(
		{
			"beneficiary_base_url": base,
			"beneficiary_token_url": f"{base}/token",
			"beneficiary_client_id": "da-services",
			"beneficiary_client_secret": "e2e-only",
			"bot_base_url": base,
			"bot_token_url": f"{base}/token",
			"bot_client_id": "da-services",
			"bot_client_secret": "e2e-only",
			"page_size": 2,
		}
	)
	settings.save(ignore_permissions=True)

	if not frappe.db.exists("User", COMMS):
		frappe.get_doc(
			{"doctype": "User", "email": COMMS, "first_name": "E2E Comms", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		frappe.get_doc("User", COMMS).add_roles(C.ROLE_COMMS)
	if not frappe.db.exists("DA RBAC Assignment", {"user": COMMS, "role": C.ROLE_COMMS, "active": 1}):
		frappe.get_doc(
			{
				"doctype": "DA RBAC Assignment",
				"user": COMMS,
				"role": C.ROLE_COMMS,
				"effective_from": today(),
				"active": 1,
			}
		).insert(ignore_permissions=True, ignore_links=True)
	frappe.db.commit()

	bc_api.ensure_routes_registered()
	import frappe.api

	paths = {r.rule for r in frappe.api.API_URL_MAP.iter_rules()}
	check(
		{"/api/v1/da-services/broadcasts", "/api/v1/da-services/broadcasts/<broadcast_id>"} <= paths,
		"routes registered next to /notifications/broadcast",
	)

	def run_until_done(name, limit=120):
		deadline = time.monotonic() + limit
		while time.monotonic() < deadline:
			dispatch.run(name)
			frappe.db.commit()
			if frappe.db.get_value(dispatch.DOCTYPE, name, "state") in ("Done", "Failed"):
				return
			time.sleep(2)

	def bot_services(state):
		req = urllib.request.Request(f"{base}/_stub/bot-services/{state}", data=b"", method="POST")
		urllib.request.urlopen(req, timeout=5).read()

	def sms_rows(name):
		return frappe.get_all(
			"OAN Message",
			filters={"reference_doctype": dispatch.DOCTYPE, "reference_name": name},
			pluck="oan_status",
		)

	def deliver(name, want, limit=120):
		if NO_OAN:  # queued in the connector outbox, not yet handed to OAN Messaging
			return ["delivered" if s is None else s for s in sms_rows(name)]
		deadline = time.monotonic() + limit
		while time.monotonic() < deadline:
			outbox.dispatch_queued()
			frappe.db.commit()
			outbox.poll_statuses()
			frappe.db.commit()
			rows = frappe.get_all(
				"OAN Message",
				filters={"reference_doctype": dispatch.DOCTYPE, "reference_name": name},
				pluck="oan_status",
			)
			if rows and all(s in want for s in rows):
				return rows
			time.sleep(3)
		return rows

	# -- 3. broadcast A: Weather, SMS + Telegram -------------------------------------
	op_a = str(uuid.uuid4())
	body = (
		"Heavy rain is expected in your woreda from Saturday to Monday. Harvest ripe crops, "
		"clear drainage ditches and keep seed and fertiliser dry. Your Development Agent can advise."
	)
	# The DA backend checks the area against the session user's User Permissions.
	frappe.set_user(COMMS)
	ctx = rules.sender(COMMS)
	req = {
		"recipients": "farmers",
		"scope": "woreda",
		"scope_value": geo["woreda"],
		"channels": ["sms", "telegram"],
		"title": "Heavy rain warning",
		"body": body,
		"source": "Weather",
		"client_op_id": op_a,
	}
	out = dispatch.create(ctx, req)
	frappe.db.commit()
	check(out["state"] == "Queued" and out["sms"]["parts"] == 2, "A queued; SMS copy counted as 2 parts")
	name_a = dispatch.find(op_a)
	dispatch.run(name_a, "sms")  # the SMS job, as the background job runs it
	frappe.db.commit()
	first = frappe.get_doc(dispatch.DOCTYPE, name_a)
	check(
		first.sms_queued == 5 and not first.telegram_broadcast_id,
		"A: SMS queued for all 5 farmers with a phone before Bot Services was called",
	)
	run_until_done(name_a)
	a = dispatch.status(name_a)
	print(json.dumps({k: a[k] for k in ("state", "telegram", "sms")}, default=str), flush=True)
	check(a["state"] == "Done", "A done")
	check(
		a["telegram"]["recipients"] == 2
		and (a["telegram"]["sent"], a["telegram"]["failed"]) == (1, 1)
		and not frappe.db.get_value(dispatch.DOCTYPE, name_a, "telegram_failed"),
		"A Telegram: 2 linked, 1 sent, 1 copy failed; the channel itself did not fail",
	)
	check("delivered" not in a["telegram"] and "read" not in a["telegram"], "A Telegram: no delivered/read")
	check(
		(a["sms"]["read"], a["sms"]["skipped_on_telegram"], a["sms"]["no_phone"], a["sms"]["queued"])
		== (6, 0, 1, 5),
		"A SMS: 6 read, none skipped for Telegram, 1 without phone, 5 queued",
	)
	statuses = deliver(name_a, {"delivered"})
	check(statuses == ["delivered"] * 5, f"A: the 5 SMS were {DELIVERED} (purpose alert)")
	purposes = set(
		frappe.get_all(
			"OAN Message",
			filters={"reference_name": name_a, "reference_doctype": dispatch.DOCTYPE},
			pluck="purpose",
		)
	)
	check(purposes == {"alert"}, "A: SMS-06 Weather -> alert")

	# -- 4. broadcast B: Informational, SMS only -----------------------------------------
	op_b = str(uuid.uuid4())
	out_b = dispatch.create(
		ctx,
		{
			"recipients": "farmers",
			"scope": "woreda",
			"scope_value": geo["woreda"],
			"channels": ["sms"],
			"title": "Seed fair",
			"body": "The woreda seed fair is on Friday at the market.",
			"source": "Informational",
			"client_op_id": op_b,
		},
	)
	frappe.db.commit()
	name_b = dispatch.find(op_b)
	run_until_done(name_b)
	statuses_b = deliver(name_b, {"delivered"})
	check(
		len(statuses_b) == 5 and set(statuses_b) == {"delivered"},
		f"B: 5 SMS sent as broadcast and {DELIVERED}",
	)

	# -- 5. broadcast C: Bot Services down -------------------------------------------
	op_c = str(uuid.uuid4())
	bot_services("down")
	dispatch.create(
		ctx,
		{
			"recipients": "farmers",
			"scope": "woreda",
			"scope_value": geo["woreda"],
			"channels": ["sms", "telegram"],
			"title": "Storm warning",
			"body": "Strong wind is expected on Thursday. Secure stores and shade nets.",
			"source": "Emergency",
			"client_op_id": op_c,
		},
	)
	frappe.db.commit()
	name_c = dispatch.find(op_c)
	dispatch.run(name_c)
	frappe.db.commit()
	c = frappe.get_doc(dispatch.DOCTYPE, name_c)
	check(c.sms_queued == 5 and c.sms_cursor == "end", "C: Bot Services down, the SMS still went at once (5)")
	check(
		c.telegram_error == "unavailable" and not c.telegram_failed and c.state != "Done",
		"C: Telegram error recorded (unavailable), retried later, broadcast still open",
	)
	bot_services("up")
	run_until_done(name_c)
	c_out = dispatch.status(name_c)
	check(
		c_out["state"] == "Done" and c_out["telegram"]["sent"] == 1 and c_out["telegram"]["error"] is None,
		"C: Bot Services back, Telegram sent, broadcast Done",
	)
	check(len(sms_rows(name_c)) == 5, "C: still 5 SMS, none sent twice")

	# -- 6. replay ------------------------------------------------------------------
	again = dispatch.create(ctx, req)
	frappe.db.commit()
	check(again["duplicate"] and dispatch.find(op_a) == name_a, "A replay: original answer, nothing new")
	check(
		frappe.db.count("OAN Message", {"reference_name": name_a, "reference_doctype": dispatch.DOCTYPE})
		== 5,
		"A replay: still 5 SMS",
	)
finally:
	stub.terminate()

print("RESULT " + ("FAILED" if check.failed else "ALL PASSED"), flush=True)
sys.exit(1 if check.failed else 0)
