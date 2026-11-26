app_name = "oan_messaging_connector"
app_title = "OAN Messaging Connector"
app_publisher = "OpenAgriNet"
app_description = "Send SMS through the OAN Messaging Service: outbox, dispatch and status polling"
app_email = "admin@openagrinet.org"
app_license = "MIT"

# Every caller app (A2C, Grievance, DA, Auth, ...) installs this app and calls
# oan_messaging_connector.api.queue_sms(...) from its own business events.
#
# No send_sms hook is registered here on purpose. A module that still sends through
# Frappe's built-in SMS sender may opt in from its own hooks.py:
#     send_sms = "oan_messaging_connector.sms_hook.send_sms"
# Explicit queue_sms calls remain the preferred integration (see README).

scheduler_events = {
	"cron": {
		# Safety-net dispatch (the normal path is enqueue_after_commit) and status polling.
		# Both are no-ops until OAN Messaging Settings is enabled and configured.
		"* * * * *": [
			"oan_messaging_connector.outbox.dispatch_queued",
			"oan_messaging_connector.outbox.poll_statuses",
		],
	},
	# Privacy: clear text and mask recipients of finished rows after the retention period.
	"daily": ["oan_messaging_connector.outbox.purge_content"],
}
