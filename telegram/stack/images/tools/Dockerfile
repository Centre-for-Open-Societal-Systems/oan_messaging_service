# syntax=docker/dockerfile:1
# Small Python services of the stack (standard library only): fake Telegram, fake 8028,
# the webhook gateway and the webhook setup. Runs as 10001.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY fake_telegram.py fake_8028.py tg_gateway.py set_webhook.py /tools/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /tools
