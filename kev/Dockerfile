FROM python:3.12-slim

ARG KEV_REF=90990a5fac2995b9faa3190f7d437e84f2067768

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /opt/kev
RUN git init . \
    && git remote add origin https://github.com/jaredpalmer/kev.git \
    && git fetch --depth 1 origin "${KEV_REF}" \
    && git checkout --detach FETCH_HEAD \
    && sed -i 's/host="127.0.0.1"/host="0.0.0.0"/' kev/serve.py \
    && grep -q 'host="0.0.0.0"' kev/serve.py \
    && uv sync --extra serve

ENV KEV_DTYPE=bf16
EXPOSE 8009

CMD ["sh", "-c", "if [ -n \"${KEV_DEVICE:-}\" ]; then sed -i 's/dev = default_device()/dev = os.environ.get(\"KEV_DEVICE\") or default_device()/' /opt/kev/kev/serve.py; fi; exec /opt/kev/.venv/bin/python -m kev.serve --run \"${KEV_RUN:-jaredpalmer/kev-4b}\" --port 8009"]
