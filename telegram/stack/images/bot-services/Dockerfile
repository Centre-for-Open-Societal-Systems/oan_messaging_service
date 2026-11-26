# syntax=docker/dockerfile:1
# OAN Bot Services for the local stack. Build context: the repository root; `base` is the
# repository's OAN Messaging image (root Dockerfile, the `certs` service), which already
# holds every Bot Services dependency (deploy/docker/requirements.txt, from uv.lock).
# This layer adds the package and the bot texts (telegram/contracts/text-keys.yaml, the
# default OAN_BOT_TEXT_KEYS_FILE) and, as for OAN Messaging, points certifi's file at
# /certs/trust-bundle.pem (certifi roots plus the stack's test CA): the Bot Services HTTP
# client trusts certifi only (httpx, trust_env=False) and calls OAN Messaging over HTTPS.
FROM base
USER root
COPY --chown=root:root oan_bot_services/ /app/oan_bot_services/
COPY --chown=root:root telegram/contracts/text-keys.yaml /app/telegram/contracts/text-keys.yaml
RUN ln -sf /certs/trust-bundle.pem "$(python -c 'import certifi; print(certifi.where())')"
USER 10001:10001
CMD ["python", "-m", "oan_bot_services", "api", "--host", "0.0.0.0", "--port", "8100"]
