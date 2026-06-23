# cua-service

A small HTTP daemon that wraps the [trycua/cua](https://github.com/trycua/cua)
Python SDK so the rest of the Hermes stack can spin up Linux sandboxes, run
shell commands, capture screenshots, and stream noVNC — all over a plain JSON
REST API.

## What this is

The cua Python SDK (`pip install cua`) is designed to be used directly from
Python code: `from cua import Sandbox, Image; await Sandbox.create(Image.linux(), local=True)`.
That's fine for scripts, but it's awkward from inside the Hermes gateway or
dashboard, which would need an in-process Python interpreter plus all the
cua dependencies installed.

This service runs in its own container, exposes a tiny REST surface, and
delegates to the SDK. Other services in the compose stack can call it over
HTTP with no special setup.

## The localhost quirk (and why `cua_host_fix.py` exists)

The cua SDK's `DockerRuntime` returns a `RuntimeInfo` with `host="localhost"`
after starting a sandbox container. It then calls `is_ready()`, which polls
`http://localhost:<mapped_port>/status` to wait for the in-container
`computer-server` to come up.

On most Linux hosts that just works: docker-proxy binds on the host's
loopback and forwards the port. **This host is an exception** — the host
where the Hermes stack runs is itself a container (Pterodactyl Wings on a
shared node), and `iptables` NAT rules for the default bridge are not
installed. `localhost:<port>` from the host's loopback never reaches the
container.

What DOES work: hitting the `docker0` bridge gateway IP
(`172.17.0.1:<port>`). `cua_host_fix.py` is a 100-line monkey-patch that
rewrites `info.host` to that bridge gateway before the SDK tries to connect.
Import the module *before* `import cua` and the patch is transparent:

```python
import cua_host_fix  # noqa: F401
from cua import Sandbox, Image
```

The patch is idempotent and works on hosts where the bridge gateway is
different (it auto-detects via `docker network inspect bridge`). Set
`CUA_DOCKER_HOST` to override.

If the host ever gets proper iptables NAT and `localhost:port` starts
working again, the patch is still safe — `info.host` is the same value the
SDK would have used.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET    | `/` | — | service info + active sandboxes |
| GET    | `/health` | — | docker socket reachability + patch status |
| GET    | `/sandboxes` | — | list active sandboxes |
| POST   | `/sandboxes` | `{name, image}` | create sandbox (image: `linux`/`macos`/`windows`) |
| DELETE | `/sandboxes/{name}` | — | stop + remove |
| POST   | `/sandboxes/{name}/shell` | `{command}` | `{returncode, stdout, stderr, success}` |
| GET    | `/sandboxes/{name}/screenshot` | — | base64-encoded PNG |
| GET    | `/sandboxes/{name}/vnc_url` | — | noVNC URL on the docker0 bridge gateway |

Example session (curl, on the host where the daemon listens on `:7777`):

```bash
# Create
curl -X POST http://localhost:7777/sandboxes \
  -H 'content-type: application/json' \
  -d '{"name":"hello","image":"linux"}'
# → {"name":"hello","image":"linux","start_seconds":20.1,...}

# Run a command
curl -X POST http://localhost:7777/sandboxes/hello/shell \
  -H 'content-type: application/json' \
  -d '{"command":"echo hi from cua; uname -a"}'
# → {"returncode":0,"stdout":"hi from cua\nLinux ...",...}

# Watch the desktop in a browser
curl http://localhost:7777/sandboxes/hello/vnc_url
# → {"novnc_url":"http://172.17.0.1:46785/vnc.html",...}

# Tear down
curl -X DELETE http://localhost:7777/sandboxes/hello
```

## Building

The Dockerfile lives next to the source:

```bash
docker build -t phantomic12/cua-service:latest -f cua/Dockerfile cua
```

## Wiring into the Hermes compose

Add the `cua` service to `/opt/hermes/docker-compose.yml`:

```yaml
  cua:
    build: ./cua-stack/cua        # or image: phantomic12/cua-service:latest
    image: phantomic12/cua-service:latest
    container_name: hermes-cua
    restart: unless-stopped
    network_mode: host           # required: see "localhost quirk" above
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - cua-state:/root/.cua     # persists sandbox metadata
    environment:
      - HERMES_UID=${HERMES_UID:-10000}
      - HERMES_GID=${HERMES_GID:-10000}
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:7777/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

volumes:
  cua-state:
```

Then:

```bash
docker compose up -d cua
curl http://localhost:7777/health
```

## Why `network_mode: host`

The cua service needs to talk to the host's docker daemon over
`/var/run/docker.sock` (it spawns the cua-xfce sandboxes there). It also
needs to reach the docker0 bridge gateway (172.17.0.1) to connect to the
sandboxes it creates. With bridge networking, the container would have its
own netns and its own `127.0.0.1` that can't reach the host's docker0
gateway. `network_mode: host` puts the service in the host's netns so the
host loopback IS the host loopback, and `172.17.0.1` is directly reachable.

Trade-off: the cua service is now exposed on every host interface. Don't
expose port 7777 on a public IP without putting something in front of it
(nginx with auth, Tailscale, etc.).
