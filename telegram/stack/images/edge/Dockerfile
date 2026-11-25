# syntax=docker/dockerfile:1
# Edge forwarder (see tools/edge.py). Starts as root only to install outbound firewall
# rules, then drops to uid 10001 with no capabilities before forwarding anything.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends iptables && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY edge.py edge-entry.sh /edge/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["/bin/sh", "/edge/edge-entry.sh"]
