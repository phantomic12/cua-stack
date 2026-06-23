"""cua_host_fix — patch cua-sandbox's DockerRuntime so the RuntimeInfo.host it
emits points at the docker0 bridge gateway IP (``172.17.0.1`` on this host)
instead of ``localhost``.

Background
----------
On this host the docker daemon is reachable via ``/var/run/docker.sock``,
but ports published with ``docker run -p HOST:CONTAINER`` are NOT reachable
on the host's ``localhost`` — they are only reachable on the ``docker0``
bridge gateway IP. The cua SDK's ``DockerRuntime`` hardcodes
``host="localhost"`` in the ``RuntimeInfo`` it returns, so
``HTTPTransport.connect()`` polls a port that nothing is listening on and
times out after 120s.

This module monkey-patches ``DockerRuntime.start`` and ``DockerRuntime.resume``
to rewrite the returned ``RuntimeInfo.host`` to the docker0 gateway. It is
safe to call multiple times; subsequent calls are no-ops.

Usage
-----
Import this module BEFORE importing ``cua``::

    import cua_host_fix  # noqa: F401
    from cua import Sandbox, Image

Environment overrides
---------------------
- ``CUA_DOCKER_HOST``  override the host the runtime reports (default: auto
  detect the docker0 bridge gateway by running ``docker network inspect bridge``).
- ``CUA_DOCKER_BIN``   path to the ``docker`` CLI (default: ``docker``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

_PATCHED = False
_RESOLVED_HOST: Optional[str] = None


def resolve_docker_host() -> str:
    """Return the IP that docker-published ports are reachable on from this
    process. On a normal Linux host that's the docker0 bridge gateway
    (``172.17.0.1``). On macOS / Windows or any host where docker-proxy
    listens on localhost, this falls back to ``localhost``.
    """
    global _RESOLVED_HOST
    if _RESOLVED_HOST is not None:
        return _RESOLVED_HOST

    override = os.environ.get("CUA_DOCKER_HOST")
    if override:
        _RESOLVED_HOST = override
        return _RESOLVED_HOST

    docker_bin = os.environ.get("CUA_DOCKER_BIN", "docker")
    if not shutil.which(docker_bin):
        # No docker CLI available — fall back to localhost and let the SDK
        # fail loudly if it really is unreachable.
        _RESOLVED_HOST = "localhost"
        return _RESOLVED_HOST

    try:
        out = subprocess.run(
            [docker_bin, "network", "inspect", "bridge",
             "--format", "{{(index .IPAM.Config 0).Gateway}}"],
            capture_output=True, text=True, timeout=5,
        )
        gw = out.stdout.strip()
        if out.returncode == 0 and gw:
            _RESOLVED_HOST = gw
            return _RESOLVED_HOST
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass

    _RESOLVED_HOST = "localhost"
    return _RESOLVED_HOST


def apply_patch() -> str:
    """Install the monkey-patch in cua_sandbox. Idempotent. Returns the
    resolved docker host IP for logging."""
    global _PATCHED
    if _PATCHED:
        return resolve_docker_host()

    from cua_sandbox.runtime import docker as _docker_mod
    from cua_sandbox.runtime import base as _base_mod

    # Wrap DockerRuntime.is_ready so it rewrites info.host BEFORE polling.
    # is_ready is called inside start() with an info object whose host is
    # "localhost" — by the time start() returns and we'd get a chance to
    # rewrite host, is_ready has already polled the wrong IP for 120s.
    _orig_is_ready = _docker_mod.DockerRuntime.is_ready

    if not getattr(_docker_mod.DockerRuntime, "_cua_is_ready_patched", False):
        async def _patched_is_ready(self, info, timeout: float = 120):
            info.host = resolve_docker_host()
            return await _orig_is_ready(self, info, timeout)

        _docker_mod.DockerRuntime.is_ready = _patched_is_ready  # type: ignore[assignment]
        _docker_mod.DockerRuntime._cua_is_ready_patched = True  # type: ignore[attr-defined]

    # Also wrap start/resume so the RuntimeInfo handed back to callers has
    # the right host (used by subsequent SDK calls like the HTTP transport
    # connect).
    _orig_start = _docker_mod.DockerRuntime.start

    if not getattr(_docker_mod.DockerRuntime, "_cua_start_patched", False):
        async def _patched_start(self, image, name, **opts):
            info = await _orig_start(self, image, name, **opts)
            info.host = resolve_docker_host()
            return info

        _docker_mod.DockerRuntime.start = _patched_start  # type: ignore[assignment]
        _docker_mod.DockerRuntime._cua_start_patched = True  # type: ignore[attr-defined]

    # Same for resume.
    if hasattr(_docker_mod.DockerRuntime, "resume"):
        _orig_resume = _docker_mod.DockerRuntime.resume

        if not getattr(_docker_mod.DockerRuntime, "_cua_host_fix_resume_wrapped", False):
            async def _patched_resume(self, image, name):
                info = await _orig_resume(self, image, name)
                if info is not None:
                    info.host = resolve_docker_host()
                return info

            _docker_mod.DockerRuntime.resume = _patched_resume  # type: ignore[assignment]
            _docker_mod.DockerRuntime._cua_host_fix_resume_wrapped = True  # type: ignore[attr-defined]

    _PATCHED = True
    return resolve_docker_host()


# Auto-apply on import.
docker_host: str = apply_patch()
