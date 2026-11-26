#!/usr/bin/env sh
# Stop the stack and wipe its data: databases and certificates.
# Only this project's (oan-tg) containers, volumes and networks are touched.
# .env is kept, except the OAN caller tokens, which no longer exist after the wipe.
set -eu
cd "$(dirname "$0")"
docker compose --profile setup down --volumes --remove-orphans
if [ -f .env ]; then
  python3 - <<'PY'
import re
p = ".env"; s = open(p).read()
s = re.sub(r"(?m)^OAN_MSG_BOT_SERVICES_TOKEN=.*$", "OAN_MSG_BOT_SERVICES_TOKEN=", s)
open(p, "w").write(re.sub(r"(?m)^OAN_MSG_SMS_TEST_TOKEN=.*$", "OAN_MSG_SMS_TEST_TOKEN=", s))
PY
fi
echo "stack data wiped; run ./up.sh"
