# syntax=docker/dockerfile:1
# OAN Messaging runtime image (MSG-08). One image, several components; the command picks
# the component (see docs/runbook.md and the Helm chart):
#   API:      uvicorn --factory oan_messaging.runtime.api:create_app --host 0.0.0.0 --port 8000
#   worker:   python -m oan_messaging.runtime worker
#   receipts: python -m oan_messaging.runtime receipts
#   cleanup:  python -m oan_messaging.runtime cleanup
#   migrate:  alembic -c migrations/alembic.ini upgrade head   (owner role)
# Dependencies are installed from deploy/docker/requirements.txt, exported from uv.lock
# with hashes (pip --require-hashes). No secrets, keys or certificates are baked in.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY deploy/docker/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-deps --require-hashes -r /tmp/requirements.txt

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 oan \
    && useradd --system --uid 10001 --gid oan --no-create-home --shell /usr/sbin/nologin oan
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root oan_messaging/ /app/oan_messaging/
COPY --chown=root:root migrations/ /app/migrations/
USER 10001:10001
EXPOSE 8000 8443
CMD ["uvicorn", "--factory", "oan_messaging.runtime.api:create_app", "--host", "0.0.0.0", "--port", "8000"]
