# OAN Messaging Service: Helm chart (draft, MSG-08H)

| | |
|---|---|
| Chart | `deploy/helm/oan-messaging/` (version `0.1.0-draft.2`, contract 3.0.0) |
| Status | **Draft for review.** It renders and validates, and `ci/run-regressions.sh` repeats every check. With the MSG-08 image and entry points it was installed, recovered and upgraded on a throwaway local kind cluster; it has not been deployed to any shared environment. |
| Baseline | Contract 3.0.0. The service (including the MSG-07b reliability work and MSG-08 runtime) is in independent review. |
| Scope | Standalone Python service on Kubernetes with **external PostgreSQL**. Not a Frappe bench; no MariaDB, Redis or bundled database. |

Nothing in the chart creates a Secret, contains a credential or private certificate, or
points at a real environment. Hostnames, CIDRs and Secret names are placeholders.

## 1. What the code provides today

Every command, variable and path the chart uses was read from the code. Where the code
has nothing, the chart refuses to render instead of inventing it.

| Need | In the code | Chart behaviour |
|---|---|---|
| API process | `uvicorn --factory oan_messaging.api:create_app` (`oan_messaging/api/__init__.py:3`) | Default `api.command`, plus `--host/--port` and `--timeout-graceful-shutdown 20` |
| Schema migration | `migrations/alembic.ini` + `migrations/env.py`, URL from `OAN_MSG_DATABASE_URL` | Default `migration.command: alembic -c migrations/alembic.ini upgrade head` (working directory: `image.workingDir`, MSG-08) |
| Storage settings | `StorageSettings`, prefix `OAN_MSG_` (`storage/config.py`) | `OAN_MSG_DATABASE_URL`, `_CONTENT_KEYS`, `_FINGERPRINT_KEYS`, `_CREDENTIAL_PEPPER` from Secrets; `_CONTENT_KEY_CURRENT`, `_FINGERPRINT_KEY_CURRENT`, `_MAX_BODY_BYTES`, `_DB_POOL_SIZE`, `_DB_MAX_OVERFLOW`, `_POLICY_*_SECONDS` as plain config |
| 8028 adapter settings | `Sms8028Settings.from_environ` (`adapters/sms8028.py:142`) | `OAN_MSG_SMS8028_CLIENT_SECRET` (Secret), `_CLIENT_CERT`, `_CLIENT_KEY`, `_CA_CERT` (file paths), `_BASE_URL` (optional) |
| Telegram tokens | `TelegramBotRegistry.from_environ`, `OAN_MSG_TELEGRAM_BOT_TOKEN__<KEY>` (`adapters/telegram.py:147`) | One env var per entry in `secrets.telegram.bots` |
| Worker process | `Worker.run_forever(stop)` library API only (`worker/dispatcher.py:142`). **No entrypoint**, no settings loader for `WorkerConfig`, adapters or the limiter | `worker.enabled: false`; enabling without `worker.command` fails rendering |
| Cleanup | `run_cleanup` (one pass) and `cleanup_forever` (loop) library functions (`worker/cleanup.py`). **No entrypoint** | `cleanup.enabled: false`; CronJob or Deployment mode once a command exists |
| Receipt ingress | `CallbackServer`, `build_server_ssl_context`, `load_pins` (`receipts/ingress.py`). **No entrypoint and no settings loader** (listen address, port, certificate paths) | `receipts.enabled: false`; files are mounted at fixed paths (§3.2) for MSG-08 to wire |
| Health endpoints | **None.** The API has only the three message routes; anything else is `404` | TCP-socket probes only (§3.6) |
| Key-file validator | `build_ssl_context` and `build_server_ssl_context` reject a private key with any group/other bit (`st_mode & 0o077`) | Validated 0600 snapshot of one Secret generation via init container (§3.2) |

## 2. Components

| Component | Kind | Default | Secrets it receives |
|---|---|---|---|
| `api` | Deployment ×2, ClusterIP Service, PDB, optional Ingress | enabled | database runtime URL, storage keys |
| `migration` | Helm `pre-install,pre-upgrade` hook Job + hook ServiceAccount | enabled | **migration database URL only** |
| `worker` | Deployment ×2 | disabled (MSG-08) | storage keys, runtime URL, 8028 client secret, 8028 client certificate/key, Telegram tokens |
| `receipts` | Deployment ×2, L4 Service (LoadBalancer by default), PDB | disabled (MSG-08) | storage keys, runtime URL, receipt server certificate/key, pinned 8028 certificates |
| `cleanup` | CronJob (default) or Deployment ×1 | disabled (MSG-08) | storage keys, runtime URL |

Every component reads the storage keys because `StorageSettings` requires all of them at
startup and the repository needs the cipher and fingerprinter. The 8028 and Telegram
credentials reach the worker only; the receipt server key reaches the receipt ingress only.

## 3. Design decisions

### 3.1 Receipt TLS stays inside the process

The receipt ingress authenticates 8028 by mutual TLS performed in-process (pinned client
certificate, `CERT_REQUIRED`, plus a fingerprint check against the TLS session). The code
has no path that reads a certificate from a header. The chart therefore:

* exposes the receipt ingress only through a **TCP Service** (`receipts.service`,
  LoadBalancer by default, with `externalTrafficPolicy: Local` and
  `loadBalancerSourceRanges`), never through an HTTP Ingress or gateway;
* **fails rendering** if a receipts Service annotation would make the load balancer
  terminate or re-encrypt TLS (`*ssl-cert*`, `*ssl-ports*`, `*ssl-negotiation-policy*`,
  `*backend-protocol*`);
* exposes only `/api/v1/messages` through the optional API Ingress.

Alternatives that keep the peer certificate, if a LoadBalancer is not available:
ingress-nginx `--enable-ssl-passthrough` with the `ssl-passthrough` annotation, or an
APISIX stream (L4) route. Both are operator configuration outside this chart; neither
may terminate TLS.

### 3.2 Private keys: a validated 0600 snapshot of ONE Secret generation

Kubernetes Secret volume files are root-owned. Without `fsGroup` a non-root process
cannot read them; with `fsGroup` they become group-readable (`0440`), which the code's
validator rejects. The worker and receipt pods therefore run an init container
(same non-root uid, no capabilities, read-only root filesystem, `fsGroup` = app gid) that
executes `files/tls_snapshot.py`, embedded verbatim with `.Files.Get`:

1. **Pin one generation.** The kubelet projects a Secret as `..data -> ..<timestamp>/`
   and switches that symlink atomically on update. Opening `tls.crt` and `tls.key`
   separately could cross the switch (old certificate, new key: reproduced in the
   regression fixture). The script resolves `..data` **once**, reads every file through
   that generation directory, and confirms `..data` still points to it afterwards.
2. **Retry the whole set** if the generation switched or vanished during the read (5
   attempts, 1 s apart). After the last attempt it exits non-zero and publishes nothing,
   so the pod does not start. A volume without `..data` is refused (fail closed).
3. **Validate before publishing.** Files are written `0600` (directory `0700`) into a new
   `.gen-<random>` directory in the memory-backed emptyDir, the key is checked for
   group/other bits, `ssl.load_cert_chain` must accept the certificate/key pair, and the
   CA (if any) must load. A mismatched pair is never published.
4. **Publish atomically.** `current -> .gen-<random>` is switched with `os.replace` of a
   symlink. Readers only ever see the previous complete set or the new one. Stale
   generations and temporary links from an interrupted earlier run are removed after a
   successful publish, so an init-container rerun (pod restart) is safe.

The main container mounts only the emptyDir, read-only, and never the Secret that holds a
private key. A main-container restart keeps the published set; a new set is taken only
when the init container runs again (pod restart or rollout).

Mounted paths:

| Pod | Path | Content |
|---|---|---|
| worker | `/run/oan-msg/tls/current/tls.crt`, `tls.key`, `ca.crt` | 8028-issued client certificate and key; optional 8028 server CA. Wired to `OAN_MSG_SMS8028_CLIENT_CERT/_CLIENT_KEY/_CA_CERT` |
| receipts | `/run/oan-msg/tls/current/tls.crt`, `tls.key` | server certificate presented to 8028 |
| receipts | `/etc/oan-msg/receipts/pins/<key>` | pinned 8028 client certificates (public; mounted directly) |

Regression coverage (`ci/regression/test_tls_snapshot.py`, 10 tests, run against the
script file itself): the naive mixed-pair hazard, single generation, a switch between
reads (one retry, new generation published), a generation deleted mid-read, continuous
churn (fails, previous set intact, no leftovers), a mismatched pair (never published),
no `..data` (fail closed), restart rerun with crash leftovers, the optional CA, and the CLI
exactly as embedded (exit codes, no key material in output). Kubelet behaviour itself was
not exercised on a cluster.

### 3.3 Migration hook: ordering, networking and upgrade lifecycle

* **What it is.** A `pre-install,pre-upgrade` hook Job for this release: one pod,
  `parallelism: 1`, `completions: 1`, `backoffLimit: 0`, `activeDeadlineSeconds: 600`.
  Alembic runs each migration in its own transaction (`transaction_per_migration=True`).
* **What runs alongside it.** On **install** the Job completes before Helm creates any
  workload. On **upgrade** the previous release's workloads keep running while the hook
  runs, and are replaced only afterwards. Every migration must therefore be backward
  compatible with the running version: expand first (add tables, nullable columns), and
  contract (drop, rename, tighten) only in a later release once no old pod uses it.
* **What serializes it.** Helm's release lock stops two operations on the same release
  from running hooks concurrently. That is **not** a database lock: two releases (for
  example two namespaces) or a manual `alembic upgrade` against the same database can run
  at once. Rule: one release per database, and no manual migrations while a release
  operation is in progress. A database-level guard (for example a PostgreSQL advisory
  lock taken in `migrations/env.py`) is runtime work for MSG-08; this chart does not
  change `migrations/`.
* **Credentials.** The hook has its own ServiceAccount (weight −10) and receives only the
  migration database URL (`database.migration.*`, ideally a separate schema-owner role).
  No storage keys, no ConfigMap, no runtime credentials. The regression checks assert
  this.
* **Networking.** The chart's ordinary NetworkPolicies do not exist yet on first install
  and are the previous release's during pre-upgrade, so they cannot serve the hook.
  `migration.network.mode` makes the path explicit and is validated at render time:

| Mode | Meaning | Render-time rule |
|---|---|---|
| `hook` | The chart creates `<fullname>-migration-egress`, a hook NetworkPolicy (weight −5, before the Job at 0) selecting only the migration pod: no ingress; egress to DNS and `networkPolicy.egress.postgres.cidrs:port`. Recreated with the new values on every upgrade (`before-hook-creation`), deleted after the hook phase succeeds, kept after a failure for debugging. | requires `networkPolicy.enabled` and non-empty `networkPolicy.egress.postgres.cidrs` |
| `preprovisioned` | An operator-managed policy already lets pods labelled `app.kubernetes.io/component=migration` reach PostgreSQL. Its name is recorded on the Job (`oan.coss/migration-network-policy`); Helm cannot verify it. | requires `migration.network.preprovisionedPolicy` |
| `unrestricted` (default) | The namespace has no egress restriction for this pod. | refused when `networkPolicy.egress.enabled` is true |

  NetworkPolicy enforcement by the CNI may lag a few seconds behind object creation. With
  `backoffLimit: 0` a first-connection failure fails the upgrade without having changed
  the schema; raise `migration.backoffLimit` if this is seen on the target cluster.

### 3.4 Retention and cleanup execution

`run_cleanup` first recovers stale `sending` rows (they become `unknown`, never resent),
then calls `purge_due` in bounded batches until a batch touches nothing. Every step is
conditional SQL, idempotent and safe to overlap, and it resumes after an outage. The
retention rules themselves are enforced by the accepted storage (content purge, binding
release after the guarantee window, receipt fingerprint and evidence expiry).

| Mode | Resource | Runs |
|---|---|---|
| `cleanup.mode: cronjob` (default) | CronJob, `concurrencyPolicy: Forbid`, `backoffLimit: 0`, `activeDeadlineSeconds: 600` | one pass per `cleanup.schedule` (default `*/5 * * * *`, matching `purge_sweep_interval` = 5 min) |
| `cleanup.mode: deployment` | Deployment ×1 | `cleanup_forever`, every `purge_sweep_interval` |

The policy's target, content gone within `worker_lease + purge_sweep_interval`, assumes
the schedule matches `config.policy.purgeSweepInterval`. Keep them aligned. Key retirement
is never automatic (`retire_key_version` refuses while data still uses a version).

### 3.5 Non-root, least-privilege workloads

* Pods: `runAsNonRoot`, uid/gid `10001` (the image must contain it), seccomp
  `RuntimeDefault`, `automountServiceAccountToken: false`, `enableServiceLinks: false`.
* Containers: no privilege escalation, read-only root filesystem (`/tmp` is a 64 Mi
  emptyDir), all capabilities dropped, requests and limits on every container including
  the init container. `PYTHONDONTWRITEBYTECODE=1`.
* Secrets: referenced by name and key only (`secretKeyRef`), per component (§2). No
  `envFrom` on a Secret, so a component never receives keys it does not use.
* NetworkPolicy (`networkPolicy.enabled: true`):
  * API ingress only from `networkPolicy.api.from` (empty means nothing can reach it);
  * receipt ingress only from `networkPolicy.receipts.fromCIDRs` (8028 source ranges);
  * no ingress at all to the worker, cleanup and migration pods;
  * optional egress allow-list (`networkPolicy.egress.enabled`, off by default until the
    destinations are known): DNS + PostgreSQL for all pods, and 8028 (`:3400`) and
    Telegram (`:443`) for workers only.

### 3.6 Probes and graceful shutdown

* **API:** `tcpSocket` startup, readiness and liveness probes on the HTTP port. This
  proves only that uvicorn listens. Startup already fails if secrets or policy are
  invalid, but the database is not checked: `make_engine` connects lazily. A real
  `/livez` and `/readyz` are MSG-08 work (or a contract decision, since they would be new
  routes).
* **Receipts:** `tcpSocket` probes on the TLS port. A bare TCP connect never completes a
  TLS handshake; check on the cluster whether asyncio logs each probe as a handshake
  error, and disable the probes (`receipts.probes: {}`) if it does.
* **Worker, cleanup:** no probes. They open no port and write no heartbeat. MSG-08 must
  add one (for example a heartbeat file plus an `exec` probe) before the chart can add
  probes.
* **Shutdown:** uvicorn handles SIGTERM (`--timeout-graceful-shutdown 20`, grace period
  30 s). The worker's `run_forever(stop)` stops on an `asyncio.Event`; the MSG-08 entrypoint
  must set it on SIGTERM. `worker.terminationGracePeriodSeconds` (default 150) must be ≥ the
  worker lease (default 120 s, `config.policy.workerLease`), and rendering fails otherwise.
  A stopped worker that does not finish within the lease leaves its claim to lease
  recovery: an interrupted `sending` becomes `unknown`, never a resend.

### 3.7 Connection estimates and budget

The chart computes two **estimates from configured limits**. Nothing at runtime enforces
them: they are only as good as the values (and the MSG-08 runtime honouring them).

* Per pod: `pool.size + pool.maxOverflow` (`OAN_MSG_DB_POOL_SIZE`,
  `OAN_MSG_DB_MAX_OVERFLOW`); workers add `worker.coordinationConnections` for the rate
  limiter's own non-pooled connections.
* **Steady state:** every enabled pod at its limit; one cleanup pod (CronJob with
  `concurrencyPolicy: Forbid`, or a Deployment with `strategy: Recreate`); no migration.
* **Upgrade/rollout peak:** per Deployment `(2 × replicas + maxSurge)` pods. The
  controller keeps at most `replicas + maxSurge` non-terminating pods, but terminating
  pods are not counted and keep their connections until they exit (bounded by
  `terminationGracePeriodSeconds`: 30 s API/receipts, 150 s workers), so up to `replicas`
  old pods can still be connected. All Deployments may roll at once. Plus one cleanup pod
  and **1** for the migration hook, which runs while the previous workloads are still up.

```text
steady  = Σ replicas × perPod            + cleanup.perPod
rollout = Σ (2 × replicas + maxSurge) × perPod + cleanup.perPod + 1 (migration)
```

| Configuration | Steady | Rollout |
|---|---|---|
| defaults (API only + migration) | 20 | 51 |
| all components (`ci/02`) | 54 | 133 |

Both figures are printed in the install NOTES. When `database.connectionBudget` > 0,
rendering fails if the **rollout** estimate exceeds it (the regression checks cover the
exact boundary: 133 renders, 132 is refused). Inputs are validated as integers:
`replicas ≥ 0`, `rollout.maxSurge ≥ 0`, `pool.size ≥ 1`, `pool.maxOverflow ≥ 0`
(SQLAlchemy treats 0 and −1 as unlimited), `coordinationConnections ≥ 1`, budget ≥ 0.

Not bounded by the estimate: a pod that is stuck terminating beyond its grace period
(force deletion, node failure), manual `kubectl` scaling, `kubectl rollout restart` of
several components while an upgrade is also running, and connections of other
applications sharing the server.

`worker.coordinationConnections` is a **budget input only**: no setting passes it to the
runtime. The MSG-08 worker entrypoint must construct `SharedRateLimiter` with the same
`max_coordination_connections` (code default 2).

**No transaction-mode PgBouncer in front of the workers.** The rate limiter relies on
session advisory locks (`pg_try_advisory_lock`), which a transaction-mode pooler breaks.
Session mode, or direct connections, are fine.

### 3.8 Certificate and key rotation

| Material | Rotation |
|---|---|
| 8028 client certificate/key (`secrets.sms8028.tlsSecret`) | Update certificate and key **in one Secret update** (one generation), then `kubectl rollout restart deployment/<fullname>-worker`. The snapshot is taken by the init container at pod start; running pods keep their published pair until restarted. A Secret updated in two steps (certificate, then key) produces an intermediate mismatched generation; pods starting at that moment fail validation and retry rather than run with it. |
| Receipt server certificate (`secrets.receipts.serverTlsSecret`) | Same, for the receipts Deployment. Tell 8028 in advance if it pins our server certificate. |
| Pinned 8028 client certificates (`secrets.receipts.pinnedCertsSecret`) | Add the new certificate as a second key (`pinnedCertKeys: [current.pem, previous.pem]`), roll out, wait until 8028 has switched, then remove the old one and roll out again. `load_pins` reads pins at startup only. The renewal notice period is an open 8028 question. |
| Storage keys (`OAN_MSG_CONTENT_KEYS`, `_FINGERPRINT_KEYS`) | Keyrings: add the new version, change `config.contentKeyCurrent` / `fingerprintKeyCurrent`, roll out all components together (one ConfigMap), and retire the old version only when `retire_key_version` allows it. |
| Caller credential pepper, database passwords | Update the Secret and restart; a pepper change invalidates every issued caller credential. |

Changing a Secret does not restart pods on its own: the chart does not own the Secrets,
so it cannot add a checksum annotation for them. ConfigMap changes do restart pods
(`checksum/config`).

## 4. Configuration

### 4.0 Names

Names are never truncated. Truncation could erase the component suffix and make two
resources (including the migration hook's) collide. The prefix (`fullnameOverride`, or
`<release>-<chart or nameOverride>`) must be a DNS-1123 label of **at most 39
characters**. That leaves room for the longest suffix, `-worker-providers-egress` (24),
under the 63-character Service limit and the 52-character CronJob limit. Longer or
invalid prefixes fail rendering with a message asking for a shorter `fullnameOverride`;
`nameOverride` must be a DNS-1123 label of at most 63 characters. The regression checks
render every values file with a 53-character release name (Helm's maximum), with
`nameOverride`, with a 39-character `fullnameOverride`, and with a release containing the
chart name. For each they assert:

* every kind/name is unique across hook and regular resources;
* names and label values stay within their limits;
* every workload keeps its component suffix;
* every Service, PodDisruptionBudget and component NetworkPolicy selects exactly its own
  component's pods.

### 4.1 Secrets to create before installing (operators, out of band)

| Values key | Type | Keys |
|---|---|---|
| `database.existingSecret` | Opaque | `runtime-url`: `postgresql+psycopg://<dml-role>:<password>@<host>:5432/<db>?sslmode=verify-full` |
| `database.migration.existingSecret` (defaults to the one above) | Opaque | `migration-url` (DDL/owner role, same format) |
| `secrets.core.existingSecret` | Opaque | `content-keys` (`v1:<base64 32 bytes>[,v0:…]`), `fingerprint-keys` (`k1:<base64 ≥32 bytes>[,…]`), `credential-pepper` (`<base64 ≥32 bytes>`) |
| `secrets.sms8028.existingSecret` | Opaque | `client-secret` |
| `secrets.sms8028.tlsSecret` | kubernetes.io/tls | `tls.crt`, `tls.key` (8028-issued client certificate); optional CA key named by `caKey` |
| `secrets.telegram.existingSecret` | Opaque | one key per bot, mapped in `secrets.telegram.bots` (bot key `^[a-z0-9][a-z0-9_-]{0,63}$`) |
| `secrets.receipts.serverTlsSecret` | kubernetes.io/tls | `tls.crt`, `tls.key` (server certificate 8028 connects to) |
| `secrets.receipts.pinnedCertsSecret` | Opaque | PEM files named in `pinnedCertKeys` |

Never commit these, put them in values files, or pass them with `--set`.

### 4.2 Values to set for a dev environment

```yaml
image: {repository: <registry>/oan-messaging, digest: "sha256:<MSG-08 image>"}
image: {workingDir: <directory holding migrations/>}        # MSG-08 image layout
database: {connectionBudget: <share of max_connections>}
networkPolicy:
  api: {from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: <gateway ns>}}}]}
  receipts: {fromCIDRs: [<8028 callback source CIDRs>]}
receipts:
  service:
    annotations: {<L4 load balancer annotations; no TLS termination>}
    loadBalancerSourceRanges: [<8028 callback source CIDRs>]
migration:
  network: {mode: hook}      # or preprovisioned + preprovisionedPolicy, see §3.3
networkPolicy:
  egress: {enabled: true, postgres: {cidrs: [<PostgreSQL CIDR>]}}
# Only once MSG-08 provides the entrypoints:
worker:   {enabled: true, command: [<MSG-08 worker command>]}
receipts: {enabled: true, command: [<MSG-08 receipt-ingress command>]}
cleanup:  {enabled: true, command: [<MSG-08 cleanup command>]}
```

The API itself is exposed by the cluster gateway (for example an APISIX route to
`<release>-oan-messaging-api:80`, `/api/v1/messages/*` only), or with `api.ingress`.
8028 allow-lists our **egress** IP for the gateway calls (`:3400`), so the worker nodes
need a fixed egress IP (NAT gateway or egress gateway). The chart cannot provide that.

Representative values files used for validation: `deploy/helm/oan-messaging/ci/`
(`02-all-components-proposed-values.yaml` uses **proposed, non-existent** commands
purely to render every template).

## 5. Validation (2026-11-15, revision for the MSG-08H review)

Tools: helm v4.3.1, kubeconform v0.8.0, Python 3.12.13 with PyYAML 6.0.3, LibreSSL 3.3.6.
No cluster was contacted; nothing was installed or deployed. Every check below is
repeatable with one command:

```bash
PYTHON=<python with PyYAML> deploy/helm/oan-messaging/ci/run-regressions.sh
```

| Check | Result |
|---|---|
| `helm lint` (defaults + 3 values files) | 4/4 pass (INFO only: no chart icon) |
| `helm template` | 9, 19 and 7 resources |
| Secret-material scan (rendered output, values, templates) | none; no Secret objects rendered |
| `kubeconform -strict`, Kubernetes 1.29.0 / 1.31.0 / 1.33.0 | 35/35 valid each |
| `ci/regression/test_tls_snapshot.py` | 10/10 pass (also on Python 3.11 and 3.9) |
| `ci/regression/check_render.py` | 2218/2218 checks pass: names and selectors across 6 name configurations × 3 values files; 6 refused name cases (including the reviewer's 63-character repro); exact budget figures and boundaries; 9 invalid budget inputs; migration modes, ordering, selectors and credential isolation; 16 earlier guards plus 2 valid inputs |

Not checked: installation on any cluster, kubelet Secret projection, Pod Security
Admission, CNI NetworkPolicy enforcement and timing, cloud load balancer behaviour, and
the probe-log behaviour in §3.6.

## 6. Assumptions

* The MSG-08 image runs as uid/gid 10001, contains `python`, `uvicorn` and `alembic` on
  `PATH`, and has `migrations/` under `image.workingDir`.
* PostgreSQL is reachable over TLS and its CA is trusted by the image.
* 8028 can reach a public TCP endpoint and publishes its callback source CIDRs.
* All components share one ServicePolicy (one ConfigMap) and roll out together after a
  policy change.

## 7. Blockers and open items

**MSG-08 (runtime packaging), required before any install:**

1. Runtime image: non-root uid/gid, pinned dependencies, `python`/`uvicorn`/`alembic`,
   migrations path, digest to pin.
2. Worker entrypoint: settings loader for `WorkerConfig`, adapters (`Sms8028Settings`,
   `TelegramBotRegistry`) and `SharedRateLimiter`; SIGTERM wired to the stop event;
   heartbeat for probes.
3. Receipt-ingress entrypoint: settings for listen host/port, server certificate/key
   paths and pin paths (the chart mounts them at the §3.2 paths), plus SIGTERM handling.
4. Cleanup entrypoint: one-shot (`run_cleanup`) for the CronJob and/or loop
   (`cleanup_forever`) for the Deployment mode.
5. Health endpoints for the API (`/livez`, `/readyz` with a database check). New routes
   may need a contract decision.
6. Worker rate limiter constructed with `max_coordination_connections` equal to
   `worker.coordinationConnections` (budget input only, §3.7).
7. Optional database-level migration guard (for example an advisory lock in
   `migrations/env.py`) if more than one release or manual migrations could ever target the
   same database (§3.3).
8. Logging configuration (the code logs ids and codes only; the runtime must not raise
   httpx/httpcore log levels in a way that bypasses the adapters' redaction filters).

**MSG-07:** independent integration and failure acceptance of the assembled service,
including migrations from an empty database and restart recovery, before this chart is
used outside a disposable dev namespace.

**Operations / open questions:**

* Which cluster hosts OAN Messaging, and its API hostname (no dev URL exists).
* A fixed egress IP for 8028, and 8028's callback source CIDRs.
* The L4 load balancer type on the target cluster (NLB, MetalLB, ingress-nginx
  pass-through or APISIX stream route).
* 8028 certificate pinning details and renewal notice period (handoff Q3).
* Database: a dedicated database with separate DDL and DML roles, `max_connections`
  share, and no transaction-mode pooler.
* Whether the API sits behind APISIX (an `ApisixRoute` is not templated here, to avoid a
  CRD dependency).
