import uuid

import frappe
from frappe.model.document import Document


class OANMessage(Document):
	def before_insert(self):
		# The Idempotency-Key: random, fixed for the row's life, never derived from the
		# row name (names restart when a site is recreated; a reused key would make OAN
		# replay an old message instead of sending the new one).
		if not self.delivery_id:
			self.delivery_id = str(uuid.uuid4())

	def on_trash(self):
		frappe.throw(frappe._("OAN Message rows are an audit trail and cannot be deleted."))
