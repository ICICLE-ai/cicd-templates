# Node backend

Use `runtime-type: node-backend` for a Node API or server-rendered application. For a static site, use the [frontend guide](../node-frontend/service-setup.md).

Start with the [shared quick start](../docs/quick-start.md#quick-start), then prepare the files below. Your framework, source layout, and CommonJS or ESM choice can stay as they are.

## Required files

| File | Purpose |
| --- | --- |
| `package.json` | Runtime dependencies and an optional `build` script. |
| `package-lock.json` | Committed npm lockfile matching `package.json`. |
| `entrypoint.sh` | Your application's startup command. |
| [`.dockerignore`](.dockerignore) | Copy this file to exclude local dependencies, secrets, and generated files. |

CI installs dependencies with `npm ci --include=dev`, runs `npm run build` when defined, then reinstalls with `npm ci --omit=dev`. Packages needed by the running server must be in `dependencies`. Commit lockfile updates whenever dependencies change.

Installs preserve lifecycle scripts; `npm-install-mode: auto` reuses dependency layers when safe and installs with full source for root hooks, workspaces, or local dependencies. Use `full` for unusual dependency hooks that read source. Non-secret `.npmrc` is supported; registry credentials in it are rejected. See [configuration](../docs/configuration.md) for cache behavior.

## Startup and routes

Set `entrypoint.sh` to the actual output path of your application:

```sh
#!/usr/bin/env sh
set -eu

# Run required initialization here.
exec node dist/main.js
```

For plain JavaScript, this might be `exec node src/main.js`. The container invokes the script with `sh`, so use POSIX shell. Keep `exec` so Node receives shutdown signals; graceful shutdown belongs in the application.

New services must read runtime `PORT`, defaulting to `5000`, and listen on `0.0.0.0`. For an existing Node HTTP server, use `server.listen(Number(process.env.PORT || "5000"), "0.0.0.0")`. The pod's networking port must match. Existing services can retain port `8000`. The image runs as the unprivileged `icicle` user with `/app` as its working directory.

New application endpoints called by a frontend must use `/api/...`, such as `/api/example/items`. Frontends call that relative path, and the shared proxy preserves the prefix and query string. Keep operational health at `GET /health`.

## Health response

Return HTTP 200 with JSON containing `status: "ok"` and the image's `BUILD_SHA`. CI uses these values to verify rollout. Set `Cache-Control: no-store`, and keep this endpoint fast. Additional health fields are optional.

For example, this CommonJS helper can serve the `/health` route in an existing Node HTTP server. Adapt imports for an ESM application or use your framework's response API.

```js
const { readFileSync } = require("node:fs");
const packageVersion = JSON.parse(readFileSync("package.json", "utf8")).version;

function sendHealth(response) {
  response.writeHead(200, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  });
  response.end(JSON.stringify({
    status: "ok",
    build_sha: process.env.BUILD_SHA || "unknown",
    version: process.env.BUILD_VERSION || packageVersion,
  }));
}
```

`BUILD_VERSION` comes from `service-version`; the package version is a fallback for local runs. Health verification checks `build_sha`, so package versioning can follow your existing process.

## Test the image

Download the Dockerfile from the same release used by your deployment workflow:

```sh
curl -fsSL \
  https://raw.githubusercontent.com/icicle-ai/cicd-templates/v2.1.0/node-backend/Dockerfile \
  -o Dockerfile

docker build \
  --build-arg NODE_VERSION=24.22.0 \
  --build-arg BUILD_SHA=local-test \
  -t myservice:test .

docker run --rm -p 8000:5000 -e PORT=5000 myservice:test
```

Match `NODE_VERSION` to your quoted `runtime-version`. Pass `--build-arg NPM_INSTALL_MODE=full` if configured. Add `--build-arg BUILD_VERSION=0.1.0` to test a release version, and pass configured build/runtime system packages as build arguments if needed.

In another terminal:

```sh
curl --fail http://localhost:8000/health
# Expect status "ok" and build_sha "local-test".
```

Also exercise an application route and stop the container to check shutdown. The downloaded Dockerfile is for local testing; CI fetches the authoritative copy. Keep shared template changes in this repository.

See [configuration](../docs/configuration.md#configuration) for schema and naming rules, [deployment](../docs/deployment.md#deployment) for pod/networking prerequisites, and [versioning](../docs/versioning.md#versioning) for upgrades.
