# Addons

| Addon | What it is | Who needs it |
|---|---|---|
| `oan_messaging_connector/` | Frappe app: the **OAN Message** outbox, post-commit dispatch, idempotency key, status polling, purge and the OAN Messaging Settings page. Imported from `oan_messaging_connector` `develop` (96fc5d1) with its history; refreshed to the connector repository's `staging` (266809a). It is not shipped in the service image. Refresh it by copying that repository's files again. | Frappe callers: A2C Credit, Grievance, DA, Auth. Not the Consent Manager or eSignet. |

The connector's contract target is stated in its own README (3.1.0 when imported); the
service contract is in `docs/openapi.yaml`. Check they agree before a module depends on it.

## Installing the connector in a Frappe bench

Frappe's `bench get-app` expects the app at the root of a repository. The connector lives
in a subfolder here, so install it with pip instead, then add it to the site:

```
pip install "git+https://github.com/Centre-for-Open-Societal-Systems/oan_messaging_service@develop#subdirectory=addons/oan_messaging_connector"
echo oan_messaging_connector >> sites/apps.txt
bench --site <site> install-app oan_messaging_connector
```

Not yet tried in a bench image. Pin a commit or tag rather than `develop` in a caller's
image build.
