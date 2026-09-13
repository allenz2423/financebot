FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "mcp>=1.2.0" \
    "httpx>=0.27.0" \
    "uvicorn>=0.30.0"

COPY server.py /app/server.py

ENV PYTHONUNBUFFERED=1
ENV PORT=3000

EXPOSE 3000

CMD ["python", "/app/server.py"]
