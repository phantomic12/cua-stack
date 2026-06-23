# Dockerfile for the cua service — exposes a small HTTP API on top of the
# cua Python SDK so the rest of the Hermes stack can spin up Linux sandboxes,
# run shell commands, capture screenshots, and stream noVNC.
#
# Build:
#   docker build -t phantomic12/cua-service:latest -f cua-stack/cua/Dockerfile cua-stack/cua
#
# Run (matches the docker-compose.yml in /opt/hermes/):
#   docker run -d --name cua \
#     --network host \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v cua-state:/root/.cua \
#     -e HERMES_UID=10000 -e HERMES_GID=10000 \
#     phantomic12/cua-service:latest
#
# Notes:
#   * --network host matches the gateway and dashboard services in the
#     Hermes compose — the daemon shares the host's netns so it can reach
#     the docker0 bridge gateway (172.17.0.1) where docker-proxy listens,
#     and other Hermes services on the host loopback can reach it on
#     http://localhost:7777. The cua SDK hardcodes "localhost" in its
#     RuntimeInfo, which doesn't work in this environment —
#     cua_host_fix.py monkey-patches it to use the bridge gateway.
#   * The host docker socket lets the service run trycua/cua-xfce sandboxes
#     locally without QEMU.
#   * The named volume cua-state persists ~/.cua/sandboxes/ so reconnecting to
#     a sandbox survives a daemon restart.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CUA_HOST=0.0.0.0 \
    CUA_PORT=7777

# System deps — docker CLI (to query sandbox state / clean up — split out
# from docker.io on Debian, the latter only ships the daemon), curl
# (healthcheck), tini (signal handling for PID 1).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini docker.io docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Install the cua Python SDK.
# The cua-sandbox package transitively depends on pynput (via cua-auto),
# which on Linux needs ``evdev`` (a C extension that needs kernel headers).
# The full cua meta-package also pulls in cua-agent and cua-cli (for
# running agents), which we don't need. So we install cua and cua-sandbox
# with --no-deps and add only the deps that don't need a C compiler.
#
# We deliberately skip pynput/evdev — the daemon never drives the host's
# keyboard/mouse, only the in-sandbox VNC/desktop. The cua-sandbox code
# path that needs pynput is in cua_auto, used by Sandbox.create(local=True)
# for *unsandboxed* host control, which we don't exercise.
RUN pip install --no-cache-dir --no-deps \
        "cua>=0.1.6,<0.2" \
        "cua-sandbox>=0.1.11,<0.2" \
        "cua-core>=0.3.0,<0.4" \
    && pip install --no-cache-dir \
        "aiohttp>=3.9,<4" \
        "httpx>=0.27,<1" \
        "websockets>=12,<17" \
        "paramiko>=4,<5" \
        "pycdlib>=1.14,<2" \
        "vncdotool>=1.2,<2" \
        "oras>=0.2.40,<0.3" \
        "grpcio==1.78.0" \
        "protobuf==6.31.1"

# Copy our wrapper + service. The wrapper MUST be importable before `cua`
# so the host fix is in place by the time Sandbox.create() is called.
WORKDIR /opt/cua-service
COPY cua_host_fix.py /opt/cua-service/
COPY cua_service.py   /opt/cua-service/

EXPOSE 7777

# Healthcheck: hits /health which validates the docker socket AND the
# host-fix patch. A pure TCP probe wouldn't catch the iptables quirk.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:7777/health || exit 1

# tini handles SIGTERM properly (the cua SDK registers async cleanup hooks).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/cua-service/cua_service.py"]
