# Dev container patch — install the cua Python SDK

## Why

The dev container (`ghcr.io/phantomic12/dev-container`) runs alongside the
cua service (added in [`phantomic12/cua-stack`](../cua-stack)). The cua
service exposes a REST API on the shared `hermes_hermes-net` network at
`http://cua:7777`. To drive sandboxes from inside the dev container, the
cua Python SDK is useful for:

- **Programmatic access** — `httpx`/`curl` work, but the SDK gives you
  type-safe `Sandbox` and `CommandResult` objects, async support, and
  retry logic.
- **Local sandboxes** — the SDK can also start sandboxes directly via
  the host's docker socket, not just through the service. Useful when
  you're iterating on a single sandbox interactively.

## How to apply

```bash
cd ~/dev-container       # or wherever your dev-container repo lives
patch -p1 < Dockerfile.patch
git add Dockerfile
git commit -m "dev-container: install cua Python SDK"
git push
```

Or apply the change manually by adding this after line 194 in `Dockerfile`
(the `pipx virtualenv ...` block):

```dockerfile
# trycua/cua Python SDK (cua-sandbox + cua-core) for driving Linux desktop
# sandboxes via the cua service in /opt/hermes/docker-compose.yml. The
# service is exposed on hermes_hermes-net as http://cua:7777 — use httpx
# or curl from this dev container to talk to it.
RUN pip3 install --break-system-packages cua-sandbox cua-core
```

## Why not use a venv inside the dev container

`/opt/cua-venv` works fine and is what the upstream cua scripts do, but
for a one-off dev container it adds a friction step that nobody will
remember. `--break-system-packages` matches the dev container's existing
convention (the Dockerfile already uses it for `yamllint`, `playwright`,
`pipx`, etc.). If you change your mind, swap the `RUN` line for:

```dockerfile
RUN python3 -m venv /opt/cua-venv && \
    /opt/cua-venv/bin/pip install cua-sandbox cua-core
```

## Why cua-sandbox + cua-core and not the `cua` meta-package

The `cua` meta-package pulls in `cua-agent`, `cua-cli`, `cua-auto`, and
`cua-computer`. Those are useful for running agentic workflows on a
desktop, but in the dev container the only thing that matters is the
SDK that talks to the cua service. `cua-sandbox` provides `Sandbox` and
`Image`; `cua-core` provides shared types. That's all you need for
`from cua import Sandbox, Image`.
