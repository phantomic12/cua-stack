# Hermes compose patch for cua

This directory contains a copy of `/opt/hermes/docker-compose.yml` with the
`cua` service added. The cua service:

- builds from `./cua/Dockerfile` (so place the cua-stack repo at
  `~/cua-stack` or symlink it into your hermes repo as `cua-stack/`)
- bind-mounts the host's docker socket so it can spawn `trycua/cua-xfce`
  sandboxes locally
- uses `network_mode: host` to share the host's netns (matches `gateway`
  and `dashboard` in the upstream compose)
- exposes a small HTTP API on `http://localhost:7777`

## How to apply

The file at `/opt/hermes/docker-compose.yml` inside the running
`hermes-hermes-agent-1` container is the image's copy and is read-only.
The real source is in your hermes repo on the host (typically
`~/hermes/docker-compose.yml`). Three options:

1. **Apply the diff to your hermes fork.** If you maintain a fork of
   `nousresearch/hermes-agent`, copy `cua-stack/` into that repo and
   add the cua service to `docker-compose.yml` using the diff below.

2. **Drop the cua service in via an override file.** Add a
   `docker-compose.override.yml` next to your existing
   `docker-compose.yml`:

   ```yaml
   services:
     cua:
       build: ./cua-stack/cua
       image: phantomic12/cua-service:latest
       container_name: hermes-cua
       restart: unless-stopped
       network_mode: host
       volumes:
         - /var/run/docker.sock:/var/run/docker.sock
         - cua-state:/root/.cua
         - ~/.hermes:/opt/hermes-data:ro
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

   Docker compose automatically merges `docker-compose.override.yml` with
   the main file. No edit to the main compose required.

3. **Use the image directly without compose.** See the README at the
   top of the repo.

## Diff

```diff
+  cua:
+    build: ./cua-stack/cua
+    image: phantomic12/cua-service:latest
+    container_name: hermes-cua
+    restart: unless-stopped
+    network_mode: host
+    volumes:
+      - /var/run/docker.sock:/var/run/docker.sock
+      - cua-state:/root/.cua
+      - ~/.hermes:/opt/hermes-data:ro
+    environment:
+      - HERMES_UID=${HERMES_UID:-10000}
+      - HERMES_GID=${HERMES_GID:-10000}
+    healthcheck:
+      test: ["CMD", "curl", "-fsS", "http://localhost:7777/health"]
+      interval: 30s
+      timeout: 5s
+      retries: 3
+      start_period: 15s
+
+volumes:
+  cua-state:
```
