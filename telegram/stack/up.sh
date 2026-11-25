#!/usr/bin/env bash
# Build and start the local Telegram stack, then set it up by script (idempotent).
#   ./up.sh            build missing images, start, set up
#   ./up.sh --rebuild  rebuild every image first (after code changes)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "run ./init-env.sh first"; exit 1; }
dc() { docker compose "$@"; }
envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
setenv() {
  python3 - "$1" "$2" <<'PY'
import re, sys
p = ".env"; s = open(p).read()
s = re.sub(rf"(?m)^{sys.argv[1]}=.*$", f"{sys.argv[1]}={sys.argv[2]}", s)
open(p, "w").write(s)
PY
}
oan() { dc --profile setup run --rm -T oan-setup python -m oan_messaging.runtime "$@"; }

echo "== build images"
for svc in certs oan-api bot-services stubs fake-telegram edge; do
  img=$(dc config --images "$svc" 2>/dev/null | sed -n 1p)
  if [ "${1:-}" = "--rebuild" ] || ! docker image inspect "$img" >/dev/null 2>&1; then
    dc build "$svc" >/dev/null || { echo "build of $svc failed; rerun: docker compose build $svc"; exit 1; }
  fi
done

echo "== database, fakes and stubs"
dc up -d --wait --wait-timeout 300 oan-db fake-telegram fake-8028 tg-gateway stubs

echo "== OAN Messaging: database, callers, credentials"
dc up -d --wait --wait-timeout 300 oan-api
oan caller --caller bot-services --channels telegram \
    --purposes transactional,alert,broadcast --bot oan >/dev/null
# A test caller standing in for the DA backend's SMS copy (SMS broadcasts stay closed, so
# Emergency copies use purpose alert).
oan caller --caller sms-test --channels sms --purposes alert,transactional >/dev/null
oan resume --channel telegram >/dev/null
oan resume --channel sms >/dev/null
for pair in bot-services:OAN_MSG_BOT_SERVICES_TOKEN sms-test:OAN_MSG_SMS_TEST_TOKEN; do
  caller=${pair%%:*}; var=${pair#*:}
  if [ -z "$(envval "$var")" ]; then
    token=$(oan issue-credential --caller "$caller" | tail -1)
    [ -n "$token" ] || { echo "issue-credential returned nothing for $caller"; exit 1; }
    setenv "$var" "$token"
    echo "$caller caller token stored in .env ($var)"
  fi
done

echo "== Telegram webhook (setWebhook with the secret)"
dc --profile setup run --rm -T tg-setup

echo "== everything else"
dc up -d --wait --wait-timeout 300 bot-migrate bot-services bot-worker \
    oan-worker-sms oan-worker-telegram oan-cleanup edge
dc ps --format 'table {{.Service}}\t{{.Status}}'
echo
echo "Tests: (cd ../.. && pytest telegram/tests)"
