# Static frontends

Use `node-frontend` for an app that builds to static HTML, CSS, and JavaScript. Node runs only during the build; Nginx serves the result. Apps that need a live Node server, including server-side rendering, use the [Node backend guide](../node-backend/service-setup.md).

## Prepare the app

Follow the shared [quick start](../docs/quick-start.md#quick-start), then set `runtime-type: node-frontend` and a quoted Node `runtime-version`, such as `"24.22.0"`, in `icicle-service.yaml`. See [configuration](../docs/configuration.md#configuration) for the schema and naming rules.

Your repository needs:

- `package.json` with a `build` script.
- A committed `package-lock.json` matching that manifest.
- A build that produces `dist/index.html` and its static assets.
- The frontend `.dockerignore` from the quick start.

The image runs `npm ci --include=dev` and `npm run build`. Build tools can be dev dependencies; no Node packages ship in the final image. Frameworks and source layouts are your choice.

Node installs preserve lifecycle scripts and automatically use full source for root hooks, workspaces, local dependencies, or source-dependent npm configuration. Set `npm-install-mode: full` for unusual dependency hooks; use the matching local build argument `NPM_INSTALL_MODE=full`. Non-secret `.npmrc` options are supported; registry credentials are rejected. See [configuration](../docs/configuration.md) for cache behavior. Check the output locally:

```bash
npm ci
npm run build
test -f dist/index.html
```

CI supplies the Dockerfile, Nginx templates, and startup script from the pinned template release. Persistent changes to those files belong in this shared repository, not your application. Keep the image's default entrypoint and command.

## Call the backend

**New frontends must use relative `/api/...` URLs.** Use the browser's `fetch` or your application's HTTP client without prepending a backend hostname or compiling `BACKEND_URL` into the browser bundle.

```javascript
export async function fetchData() {
  const response = await fetch("/api/example/items");
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}
```

Enforce this convention in application reviews and tests; shared CI does not inspect request code. Backends must expose the matching `/api/...` routes.

Set `BACKEND_URL` in the frontend container's runtime environment:

```text
BACKEND_URL=http://backend:3000
```

A request to `/api/example/items?limit=10` reaches `http://backend:3000/api/example/items?limit=10`. Nginx preserves the full path, query, method, body, and normal request headers. API errors remain API responses instead of falling back to the SPA.

The value must be an HTTP or HTTPS origin, optionally including a port and one trailing slash. Paths, credentials, query strings, fragments, and whitespace are rejected at startup.

Unset or empty `BACKEND_URL` preserves the previous static/SPA behavior, including `/api/...` falling back to the SPA. This also supports existing deployments where ingress already routes API requests. New clients still follow the relative-path convention.

## Configure the deployed frontend

The [deployment workflow](../docs/deployment.md#deployment) manages the existing pod's image. Runtime environment and networking remain platform-managed. Merge this setting into the pod's `environment_variables`, retain its other variables, and restart:

```json
{
  "environment_variables": {
    "BACKEND_URL": "https://your-backend-networking-host"
  }
}
```

Use an address reachable **from the frontend container**. `localhost` means the frontend container itself. Docker names such as `backend` require a shared network. For Tapis, use the backend's returned networking URL, normally HTTPS on port 443; its internal listening port is not necessarily exposed at that hostname.

HTTPS uses SNI, the backend hostname as `Host`, and certificate verification against the system CA bundle. Install a private CA in that trust store when required. DNS must resolve at startup; restart the frontend if the resolved backend address changes.

Nginx forwards client address, host, and scheme headers. The ingress must overwrite client-supplied forwarded headers, and backends must trust only known proxies. Configure authentication and cookies for the frontend origin; cookies scoped to the backend domain will not work through this proxy.

Adopting these templates requires one image build. After adoption, changing or removing `BACKEND_URL` needs only an environment update and restart of the same image.

Optional proxy settings are runtime environment variables; changes need a restart:

| Variable | Default | Accepted values |
|---|---|---|
| `BACKEND_MAX_BODY_SIZE_MB` | `1` | Integer 1–1024 MiB. |
| `BACKEND_CONNECT_TIMEOUT_SECONDS` | `60` | Integer 1–75 seconds. |
| `BACKEND_READ_TIMEOUT_SECONDS` | `60` | Integer 1–3600 seconds between upstream reads. |
| `BACKEND_SEND_TIMEOUT_SECONDS` | `60` | Integer 1–3600 seconds between upstream writes. |
| `BACKEND_STREAMING` | `off` | `on` / `off`; `on` disables response buffering for streams such as SSE. |
| `BACKEND_WEBSOCKETS` | `off` | `on` / `off`; enables WebSocket upgrades. |

Request buffering stays enabled. Invalid values fail startup. For long-lived streams or WebSockets, set a read timeout suitable for the backend's heartbeat interval. The ingress must also support streaming or WebSocket upgrades and allow the required connection duration.

## Test the image locally

From your application directory, fetch all build inputs from the same [template release](../docs/versioning.md#versioning) used by your workflow:

```bash
TEMPLATE_REF=v2.1.0
BASE=https://raw.githubusercontent.com/icicle-ai/cicd-templates/$TEMPLATE_REF
curl -fsSL "$BASE/node-frontend/Dockerfile" -o Dockerfile
curl -fsSL "$BASE/node-frontend/nginx.conf" -o nginx.conf
curl -fsSL "$BASE/node-frontend/nginx-api.conf.template" -o nginx-api.conf.template
curl -fsSL "$BASE/node-frontend/entrypoint.sh" -o nginx-entrypoint.sh

docker build --build-arg NODE_VERSION=24.22.0 \
  --build-arg BUILD_SHA=local-test -t myfrontend:test .
docker run --rm -d --name frontend-test -p 8000:8000 myfrontend:test
curl --retry 10 --retry-connrefused --retry-delay 1 \
  http://localhost:8000/healthcheck        # local-test
curl -I http://localhost:8000/index.html   # Cache-Control: no-store
curl http://localhost:8000/a/deep/link    # SPA HTML
docker stop frontend-test
```

To check proxying, create a Docker network and start your backend on it with `--name backend`, listening on `0.0.0.0:3000`. Then run the same frontend image:

```bash
docker network create app-network
# Start your backend with: --network app-network --name backend
docker run --rm -d --name frontend-test --network app-network \
  -p 8000:8000 -e BACKEND_URL=http://backend:3000 myfrontend:test
curl --retry 10 --retry-connrefused --retry-delay 1 \
  http://localhost:8000/healthcheck
curl 'http://localhost:8000/api/example/items?limit=10'
docker stop frontend-test
```

Use an endpoint your backend implements, with any required credentials or body. Check its logs for the complete `/api/...` path and query. Stop the first frontend before reusing port 8000. The downloaded build inputs are temporary; do not commit modified copies.

## Runtime behavior

Nginx serves `/app/site` on port `8000` as the unprivileged `icicle` user. Startup renders and validates configuration under `/tmp/nginx`; graceful shutdown lets active requests finish.

- `/healthcheck` returns the build SHA used by CI to verify rollout.
- `/assets/` gets one-year immutable caching only for a filename token separated by a dot or dash before its extension: at least eight hexadecimal characters, or at least eight base64url characters containing both a letter and digit (for example, `app.abcdef12.js` or `app-Abcd1234.js`). Matching files **must be content-versioned**: change the filename whenever the content changes. Other assets use `no-cache` and must revalidate.
- `index.html` is uncached, and unmatched frontend paths fall back to it.
- `/api` and `/api/...` go to the backend only when `BACKEND_URL` is set.

The site is hosted at `/`. Hosting under a prefix requires coordinated ingress rewriting and bundler/router base settings; API requests still use origin-root `/api/...`.

If API calls return HTML, check `BACKEND_URL` and the startup command. For startup failures or HTTP 502, check DNS, network access, port, and HTTPS certificate trust. For rollout failures, use the [shared deployment guidance](../docs/deployment.md#deployment).
