# concierge-browser — disposable headed browser for B1 login intake / read-tier.
#
# One immutable base image: Xvfb + openbox + x11vnc + noVNC + patchright
# headed Chromium.  CONCIERGE_ACCEL (igpu|cpu) branches Chromium's GL path
# at runtime only; the image itself is identical in both modes.
#
# The container is spawn-per-session by the bot (see
# src/services/concierge/browser_sessions.py build_docker_run): the browser
# profile lives on a tmpfs that dies with the container, the noVNC port binds
# to 127.0.0.1 on the host, and a one-time VNC password is injected via env.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    openbox \
    x11vnc \
    novnc \
    websockify \
    curl \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Headed Chromium via patchright (undetected-chromedriver fork).
RUN pip install --no-cache-dir patchright && \
    patchright install chromium && \
    playwright install-deps chromium 2>/dev/null || true

COPY sandbox/concierge-browser/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:99
ENV PROFILE_DIR=/tmp/profile
ENV CONCIERGE_ACCEL=cpu
ENV NOVNC_PORT=6080

EXPOSE 6080

ENTRYPOINT ["/entrypoint.sh"]