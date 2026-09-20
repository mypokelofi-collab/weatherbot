# flowbot - 15m BTC momentum bot (paper money, real market data)
#
# Two stages so the runtime image carries no compiler and no pip cache.
# Runs as an unprivileged user, writes only to /app/data, and exposes the
# dashboard on 8032.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    PATH="/opt/venv/bin:$PATH"

# The bot reasons entirely in UTC; make the container agree.
RUN ln -snf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin flowbot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY flowbot ./flowbot
COPY config ./config
COPY pyproject.toml README.md ./

# State (SQLite ledger) and recordings live here; mount a volume over it so a
# redeploy does not erase the trade history.
RUN mkdir -p /app/data/state /app/data/recordings && chown -R flowbot:flowbot /app

USER flowbot
EXPOSE 8032

# Liveness only: the endpoint answers 200 even when the market feed is
# reconnecting, because a bot that is up but waiting for data should not be
# restarted out from under its own reconnect logic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8032/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "flowbot"]
CMD ["run", "-c", "config/flowbot.yml"]
