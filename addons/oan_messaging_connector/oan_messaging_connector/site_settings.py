"""Sync `OAN Messaging Settings` into the DB from environment variables on deploy."""

from __future__ import annotations

import json
import os

import frappe
from frappe import _

SETTINGS_DOCTYPE = "OAN Messaging Settings"
SETTINGS_ENV = "OAN_MESSAGING_SETTINGS"
TOKEN_ENV = "OAN_MESSAGING_TOKEN"


def sync_settings() -> None:
	"""Set `OAN Messaging Settings` from $OAN_MESSAGING_SETTINGS (JSON map) and $OAN_MESSAGING_TOKEN.

	Empty values are skipped, so a field left out or a missing token keeps what the DB has.
	"""
	values = {
		fieldname: value
		for fieldname, value in json.loads(os.environ.get(SETTINGS_ENV) or "{}").items()
		if value not in (None, "")
	}
	if os.environ.get(TOKEN_ENV):
		values["token"] = os.environ[TOKEN_ENV]
	if not values:
		return
	meta = frappe.get_meta(SETTINGS_DOCTYPE)
	unknown = sorted(fieldname for fieldname in values if not meta.has_field(fieldname))
	if unknown:
		frappe.throw(_("{0} has no field {1}.").format(SETTINGS_DOCTYPE, ", ".join(unknown)))
	doc = frappe.get_single(SETTINGS_DOCTYPE)
	doc.update(values)
	doc.save(ignore_permissions=True)
	print(f"{SETTINGS_DOCTYPE}: {', '.join(sorted(values))} written")
