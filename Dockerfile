# Multi-stage build keeps the runtime image lean (no gcc, no headers).
FROM python:3.14-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Install only what's needed to compile any source-only wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Build dependencies into the wheels layer; we copy /root/.local across stages.
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


FROM python:3.14-slim-bookworm AS runtime

# tini provides proper PID-1 / signal handling for graceful shutdown.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 nebula \
    && useradd --system --uid 10001 --gid nebula --home /app --shell /sbin/nologin nebula

ENV PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.14/site-packages" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy compiled deps from builder.
COPY --from=builder /install /install

# Now copy the source. .dockerignore should exclude staging, .env, sessions, etc.
COPY --chown=nebula:nebula . /app

RUN mkdir -p /app/staging \
    && chown -R nebula:nebula /app

# Lock down the runtime dirs.
RUN chmod 0700 /app/staging && chmod 0750 /app

USER nebula

EXPOSE 2121
EXPOSE 60000-60009

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(2); \
s.connect(('127.0.0.1', 2121)); s.sendall(b'QUIT\r\n'); out=s.recv(64); \
sys.exit(0 if out else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["python", "main.py"]
