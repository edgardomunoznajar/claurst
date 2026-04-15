# Mock NES (enterprise search ACL) service for the Simon demo.
#
# Plays the role of NQRY's NES /acl endpoints so Simon can be tested
# end-to-end against a remote enforcer without a real NES deployment.
# Policy format is intentionally identical to simon's acl.json — see
# deploy/simon/acl.json and src-rust/crates/acl/src/static_json.rs.

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
        "fastapi==0.115.*" \
        "uvicorn[standard]==0.32.*" \
        "pydantic==2.*"

COPY app.py        /app/app.py
COPY policy.json   /etc/nes/policy.json

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; \
                   sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
