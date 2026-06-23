"""Example: drive a cua sandbox from inside the Hermes dev container.

Prereqs:
  - The ``cua`` service is running on the host (docker compose up -d cua).
  - The dev container can reach the host's loopback (it can, on this host).

Usage:
  python /opt/data/cua-stack/cua/examples/from_dev_container.py
"""
import asyncio
import json
import urllib.request

import aiohttp

CUA_URL = "http://host.docker.internal:7777"   # or the host LAN IP from the dev container
# When the dev container runs with --network host the loopback works directly:
# CUA_URL = "http://127.0.0.1:7777"


async def main():
    async with aiohttp.ClientSession() as sess:
        # 1) Health
        async with sess.get(f"{CUA_URL}/health") as r:
            health = await r.json()
            print("health:", json.dumps(health, indent=2))

        # 2) Create a sandbox
        async with sess.post(f"{CUA_URL}/sandboxes",
                             json={"name": "example-sb", "image": "linux"}) as r:
            created = await r.json()
            print("created:", created)

        # 3) Run a shell command
        async with sess.post(f"{CUA_URL}/sandboxes/example-sb/shell",
                             json={"command": "echo HELLO-FROM-CUA; cat /etc/os-release | head -2"}) as r:
            shell = await r.json()
            print("shell:", shell)

        # 4) Get the noVNC URL
        async with sess.get(f"{CUA_URL}/sandboxes/example-sb/vnc_url") as r:
            vnc = await r.json()
            print("vnc:", vnc)

        # 5) Tear it down
        async with sess.delete(f"{CUA_URL}/sandboxes/example-sb") as r:
            print("delete:", await r.json())


if __name__ == "__main__":
    asyncio.run(main())
