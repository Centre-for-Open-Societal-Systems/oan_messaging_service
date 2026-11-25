#!/usr/bin/env sh
# Stop the stack. Data volumes are kept; ./reset.sh wipes them.
set -eu
cd "$(dirname "$0")"
docker compose --profile setup down --remove-orphans
