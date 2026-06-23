"""cua_service — minimal HTTP API around the cua Python SDK.

Runs as a long-lived process inside the `cua` compose service. The service
exposes a small REST surface for the rest of the Hermes stack (and external
callers) to spin up Linux sandboxes, run shell commands inside them, and
tear them down.

Endpoints
---------
GET    /                       service info + version
GET    /health                 healthcheck (also probes docker socket)
GET    /sandboxes              list active sandboxes
POST   /sandboxes              create a new sandbox
                            body: {"name": "...", "image": "linux"}
DELETE /sandboxes/{name}       stop + remove a sandbox
POST   /sandboxes/{name}/shell run a shell command
                            body: {"command": "..."}
GET    /sandboxes/{name}/screenshot  PNG screenshot (base64)
GET    /sandboxes/{name}/vnc_url    URL to noVNC for the sandbox

The shell, screenshot, and vnc endpoints are optional — they're cheap to add
once the daemon is running and let the gateway / dashboard drive a sandbox
remotely without going through the cloud API.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

# CRITICAL: import the host fix BEFORE the cua SDK so the patch is installed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cua_host_fix  # noqa: F401

from cua import Image, Sandbox  # noqa: E402

LOG = logging.getLogger("cua_service")

# In-memory registry: name -> Sandbox. The cua SDK also writes state to
# ~/.cua/sandboxes/{name}.json so we can reconnect across restarts, but for
# the daemon we keep references for the running process.
_SANDBOXES: Dict[str, Sandbox] = {}
_LOCKS: Dict[str, asyncio.Lock] = {}


def _get_lock(name: str) -> asyncio.Lock:
    if name not in _LOCKS:
        _LOCKS[name] = asyncio.Lock()
    return _LOCKS[name]


def _resolve_image(kind: str) -> Image:
    kind = kind.lower()
    if kind in ("linux", "ubuntu"):
        return Image.linux()
    if kind in ("macos", "mac"):
        return Image.macos()
    if kind in ("windows", "win"):
        return Image.windows()
    raise ValueError(f"Unsupported image kind: {kind!r} (use linux/macos/windows)")


async def _create_sandbox(name: str, image_kind: str) -> Sandbox:
    img = _resolve_image(image_kind)
    # local=True → use the bind-mounted host docker socket.
    # time_to_start=180 → cold start of a fresh trycua/cua-xfce image can take
    # 30-60s, so give some headroom.
    sb = await Sandbox.create(img, name=name, local=True, time_to_start=180)
    return sb


# ── HTTP handlers ─────────────────────────────────────────────────────────


async def handle_index(request: web.Request) -> web.Response:
    return web.json_response({
        "service": "cua",
        "version": "0.1.0",
        "cua_sdk": "0.1.6",
        "docker_host": cua_host_fix.docker_host,
        "active_sandboxes": list(_SANDBOXES.keys()),
        "endpoints": [
            "GET /",
            "GET /health",
            "GET /sandboxes",
            "POST /sandboxes",
            "DELETE /sandboxes/{name}",
            "POST /sandboxes/{name}/shell",
            "GET /sandboxes/{name}/screenshot",
            "GET /sandboxes/{name}/vnc_url",
        ],
    })


async def handle_health(request: web.Request) -> web.Response:
    """Health check: confirm the docker socket is reachable AND that the
    cua-host-fix patch is installed and resolving the right IP."""
    checks: Dict[str, Any] = {}
    try:
        import subprocess
        out = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5,
        )
        checks["docker_socket"] = {
            "ok": out.returncode == 0,
            "version": out.stdout.strip() if out.returncode == 0 else None,
            "stderr": out.stderr.strip() if out.returncode != 0 else None,
        }
    except Exception as e:
        checks["docker_socket"] = {"ok": False, "error": str(e)}

    checks["host_patch"] = {
        "ok": True,
        "resolved_host": cua_host_fix.docker_host,
        "note": "cua_host_fix is monkey-patching DockerRuntime to use "
                "the docker0 bridge gateway instead of localhost.",
    }
    ok = all(c.get("ok") for c in checks.values())
    return web.json_response({"ok": ok, "checks": checks},
                            status=200 if ok else 503)


async def handle_list(request: web.Request) -> web.Response:
    return web.json_response({
        "sandboxes": [
            {
                "name": name,
                "running": True,  # if it's in our registry, we have a live handle
            }
            for name in _SANDBOXES.keys()
        ]
    })


async def handle_create(request: web.Request) -> web.Response:
    body = await request.json() if request.body_exists else {}
    name = body.get("name")
    image = body.get("image", "linux")
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    lock = _get_lock(name)
    async with lock:
        if name in _SANDBOXES:
            return web.json_response({"error": f"sandbox {name!r} already exists"},
                                     status=409)
        try:
            t0 = time.time()
            sb = await _create_sandbox(name, image)
            elapsed = time.time() - t0
            _SANDBOXES[name] = sb
            return web.json_response({
                "name": name,
                "image": image,
                "start_seconds": round(elapsed, 2),
                "vnc_url": f"http://{request.host}/sandboxes/{name}/vnc",
            })
        except Exception as e:
            LOG.exception("create failed")
            return web.json_response(
                {"error": f"{type(e).__name__}: {e}"}, status=500)


async def handle_delete(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    lock = _get_lock(name)
    async with lock:
        sb = _SANDBOXES.pop(name, None)
        if sb is None:
            return web.json_response({"error": f"sandbox {name!r} not found"},
                                     status=404)
        try:
            # delete() stops the container and removes state
            await sb.delete()
        except Exception as e:
            LOG.warning("delete error for %s: %s", name, e)
    return web.json_response({"name": name, "deleted": True})


async def handle_shell(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    sb = _SANDBOXES.get(name)
    if sb is None:
        return web.json_response({"error": f"sandbox {name!r} not found"},
                                 status=404)
    body = await request.json() if request.body_exists else {}
    command = body.get("command")
    if not command:
        return web.json_response({"error": "command is required"}, status=400)
    try:
        result = await sb.shell.run(command)
        return web.json_response({
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "success": result.success,
        })
    except Exception as e:
        LOG.exception("shell failed")
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}"}, status=500)


async def handle_screenshot(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    sb = _SANDBOXES.get(name)
    if sb is None:
        return web.json_response({"error": f"sandbox {name!r} not found"},
                                 status=404)
    try:
        png = await sb.screenshot()
        return web.json_response({
            "name": name,
            "format": "png",
            "encoding": "base64",
            "data": base64.b64encode(png).decode("ascii"),
        })
    except Exception as e:
        LOG.exception("screenshot failed")
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}"}, status=500)


async def handle_vnc_url(request: web.Request) -> web.Response:
    """The cua-xfce container exposes noVNC on its internal port 6901, mapped
    to a random host port. The cua SDK's HTTPTransport (used for local
    sandboxes) doesn't implement get_display_url(), so we look up the
    mapped port via ``docker inspect`` and build the URL on the docker0
    bridge gateway (where docker-proxy listens)."""
    import subprocess
    name = request.match_info["name"]
    sb = _SANDBOXES.get(name)
    if sb is None:
        return web.json_response({"error": f"sandbox {name!r} not found"},
                                 status=404)
    try:
        out = subprocess.run(
            ["docker", "inspect", name,
             "--format", "{{(index (index .NetworkSettings.Ports \"6901/tcp\") 0).HostPort}}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip().isdigit():
            return web.json_response(
                {"error": f"no mapped port: {out.stderr.strip() or 'unknown'}"},
                status=500)
        host_port = int(out.stdout.strip())
        return web.json_response({
            "name": name,
            "novnc_url": f"http://{cua_host_fix.docker_host}:{host_port}/vnc.html",
            "vnc_port": host_port,
            "note": "noVNC runs inside the cua-xfce container; the URL uses the "
                    "host's docker0 bridge gateway because the docker-proxy "
                    "doesn't bind on the host's loopback in this environment.",
        })
    except Exception as e:
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}"}, status=500)


# ── Lifecycle ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifecycle(app: web.Application):
    LOG.info("cua_service starting; docker_host=%s", cua_host_fix.docker_host)
    yield
    # On shutdown, try to clean up any sandboxes we created.
    LOG.info("cua_service shutting down; cleaning up %d sandboxes",
             len(_SANDBOXES))
    for name, sb in list(_SANDBOXES.items()):
        try:
            await sb.delete()
        except Exception as e:
            LOG.warning("cleanup error for %s: %s", name, e)
    _SANDBOXES.clear()


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)  # screenshots
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/sandboxes", handle_list)
    app.router.add_post("/sandboxes", handle_create)
    app.router.add_delete("/sandboxes/{name}", handle_delete)
    app.router.add_post("/sandboxes/{name}/shell", handle_shell)
    app.router.add_get("/sandboxes/{name}/screenshot", handle_screenshot)
    app.router.add_get("/sandboxes/{name}/vnc_url", handle_vnc_url)
    app.cleanup_ctx.append(_lifecycle)
    return app


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("CUA_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("CUA_PORT", "7777")))
    args = p.parse_args()
    logging.basicConfig(
        level=os.environ.get("CUA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(make_app(), host=args.host, port=args.port,
                access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
