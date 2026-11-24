# syntax=docker/dockerfile:1
# OAN Messaging for the local stack: the repository image (built from the root Dockerfile
# as the `certs` service, passed in as the `base` context) with one change. Its HTTP
# client trusts certifi's bundle only (httpx, trust_env=False), so certifi's file is
# pointed at /certs/trust-bundle.pem: certifi's roots plus the stack's test CA, written by
# tools/gen_certs.py. Nothing else differs from the delivered image.
FROM base
USER root
RUN ln -sf /certs/trust-bundle.pem "$(python -c 'import certifi; print(certifi.where())')"
USER 10001:10001
