FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY sandbox/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sandbox/app.py .

# Create sandbox user and system paths
RUN useradd -m -u 1000 sandbox \
    && mkdir -p /opt/packages /tmp/mplconfig /host/project /host/data /workspace \
    && chown -R sandbox:sandbox /opt/packages /tmp/mplconfig /host/project /host/data /workspace

# --- Snapshot Ingestion ---
COPY main.py /host/project/main.py
COPY src /host/project/src
COPY plaid_sync.py /host/project/plaid_sync.py
COPY sandbox_client.py /host/project/sandbox_client.py
COPY requirements.txt /host/project/requirements.txt
COPY Dockerfile /host/project/Dockerfile
COPY docker-compose.yml /host/project/docker-compose.yml
COPY sandbox/app.py /host/project/sandbox/app.py
COPY sandbox/Dockerfile /host/project/sandbox/Dockerfile
COPY README.md /host/project/README.md
COPY searxng /host/project/searxng
COPY squid.conf /host/project/squid.conf

ENV MPLCONFIGDIR=/tmp/mplconfig

USER sandbox

EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
