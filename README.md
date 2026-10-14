# ICICLE service templates

Shared builds and deployment for Python services, Node servers, and static frontends. The tested release is **`v2.0.0`**.

## Quick start

Choose a runtime, then follow its short guide:

| Runtime | Use it for | Guide | Copy into your repo |
|---|---|---|---|
| `python` | Python APIs and services | [Python setup](python/service-setup.md) | [`.dockerignore`](python/.dockerignore) |
| `node-backend` | Node APIs or frontends that need a running Node server | [Node setup](node-backend/service-setup.md) | [`.dockerignore`](node-backend/.dockerignore) |
| `node-frontend` | Static sites built by Node and served by Nginx | [Frontend setup](node-frontend/service-setup.md) | [`.dockerignore`](node-frontend/.dockerignore) |

Before deploying, the platform team must provision a Tapis pod, allow its image repository, and configure networking and registry access. Your repository needs access to the organization secrets used below.

**1. Create `icicle-service.yaml` in your service repository.** This example is for Python; change the runtime fields for Node:

```yaml
schema-version: 2
service-name: accountapi
pod-name: accountapi
service-version: 0.1.0
project-name: myproject
service-type: ai
runtime-type: python
runtime-version: "3.13"
include-dev-dependencies: false
build-apt-packages: []
runtime-apt-packages: []
```

For Node, select `node-backend` or `node-frontend` and a quoted Node version such as `"22.11"`. The frontend's Node version is used only during its build. Use the name of your actual provisioned pod.

**2. Add the files required by your runtime guide.** Copy its `.dockerignore`. Backends need an `entrypoint.sh`; the frontend receives its startup script from CI. CI supplies the authoritative Dockerfile and Nginx files, so service-local edits to downloaded copies will be replaced.

**3. Create `.github/workflows/deploy.yaml`:**

```yaml
name: Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  deploy:
    uses: icicle-ai/cicd-templates/.github/workflows/deploy-service.yaml@v2.0.0
    secrets:
      TAPIS_TOKEN: ${{ secrets.TAPIS_TOKEN }}
      REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}
      REGISTRY_PASSWORD: ${{ secrets.REGISTRY_PASSWORD }}
      HEALTHCHECK_TOKEN: ${{ secrets.HEALTHCHECK_TOKEN }}
```

`HEALTHCHECK_TOKEN` is optional, for health endpoints requiring a bearer token. The other secrets are required. You can also copy the current [starter workflow](deploy.yaml). Use the pinned workflow above when starting from older downloaded files: the original `v2.0.0` archive's starter still calls `@main`.

**4. Commit and push to your service's `main` branch.** Check the Actions run for build and rollout results.

## Configuration

- New services use **schema 2**. `service-name` and `pod-name` must be identical: 4–64 lowercase ASCII letters/digits, starting with a letter. No spaces, hyphens, or underscores. This names the registry image and Tapis pod; Tapis manages internal container identifiers.
- Existing services can retain **schema 1**, including different service/pod names. To migrate, coordinate the new image name and registry allowlist first; CI does not create or rename pods. Onboarding review must require schema 2 because CI cannot infer whether a service is new.
- `service-version` is `MAJOR.MINOR.PATCH`. `project-name` identifies the project; `service-type` is `ai`, `ci`, or `domain`. Project and type are recorded as image labels.
- Quote `runtime-version` as `MAJOR.MINOR`. Legacy unquoted decimals are accepted with a warning. Package lists contain Debian package names, optionally with architecture or exact version.
- Framework, package name, source layout, and Node module format are application choices.

| Optional field | Default | Use |
|---|---|---|
| `health-url` | Discover from the pod's default or sole HTTP endpoint | Exact HTTP(S) health URL; no credentials, query, or fragment. |
| `rollout-timeout` | `300` | Verification timeout in seconds, from 30 to 1800. |
| `include-dev-dependencies` | `true` | Python only. Set `false` for new services; omission preserves legacy dependencies. |

See the [annotated configuration](icicle-service-example.yaml) for field examples. Unknown extension fields warn and are ignored by CI; malformed values fail before building.

## Deployment

Services run as unprivileged `icicle` (UID 999) and normally listen on **`0.0.0.0:8000`**. Backends start with a POSIX shell script ending in `exec`; applications handle shutdown signals. Keep the frontend image's shared entrypoint and allow time for graceful shutdown.

New frontend API clients **must use relative `/api/...` requests**. Backends must serve the same full paths. Frontend proxying is optional for compatibility: set runtime `BACKEND_URL` to enable it, or retain existing ingress routing. See the [frontend guide](node-frontend/service-setup.md). CI validates configuration, not application route implementations.

| Runtime | Health endpoint | Required response |
|---|---|---|
| Python / Node backend | `/health` | HTTP 200 JSON with `status: "ok"` and `build_sha` equal to `BUILD_SHA`. |
| Static frontend | `/healthcheck` | HTTP 200 with the bare build SHA; provided by Nginx. |

CI also supplies `BUILD_VERSION` to backends. Prefer it for health version metadata, with package metadata as a local fallback. Health routes stay outside `/api`.

CI builds and pushes the application version tag plus a unique build tag. It updates only the pod's image, restarts it, and checks for a new instance serving the expected SHA. It records the image digest; unique tags are not registry-enforced immutability. Pod environment, networking, volumes, and resources remain platform-managed.

The Actions runner must reach the health endpoint. Health redirects are rejected; an optional `HEALTHCHECK_TOKEN` goes only to that endpoint, never in the Tapis API request. Assign one repository to each pod: deployment serialization is scoped to the repository.

A failed rollout fails CI and leaves the requested image configured. Inspect the reported previous image, pod status, and logs before retrying or rolling back. CI does not automatically roll back.

## Versioning

Pin the reusable workflow and manual template downloads to **`v2.0.0`**. CI fetches the Dockerfiles, startup files, and helpers from the workflow's exact commit. Update the pin deliberately when adopting another tested release.

These settings are independent:

| Version | Meaning |
|---|---|
| Template release (`v2.0.0`) | Shared Dockerfiles, workflow, and runtime files. |
| `schema-version` | Rules for the service configuration; this release supports 1 and 2. |
| `service-version` | Application release. |
| `runtime-version` | Python or Node interpreter/toolchain version. |

Images record `io.icicle.template.version` and `io.icicle.template.revision`. CI supplies the corresponding `TEMPLATE_VERSION` and `TEMPLATE_REVISION` build arguments; manual builds default to `development` and `unknown`. The published `v1` predates this setup. See [release notes](CHANGELOG.md).

## Maintaining the templates

Make shared build and startup changes here. Before publishing a release, run the checks below and verify deployment against a provisioned test pod:

```bash
python3 -m venv /tmp/icicle-template-tests
. /tmp/icicle-template-tests/bin/activate
python -m pip install -r scripts/requirements.txt
python -m unittest discover -s tests -v
REQUIRE_DOCKER=1 python tests/test_images.py -v
```

Use Python 3.9+, Nginx with HTTP SSL support, `envsubst`, and OpenSSL. `NGINX_BINARY` can select a local Nginx binary. Image tests require Docker with BuildKit and network access. CI runs both suites; mocked Tapis tests do not replace a live rollout check.

Update `VERSION` and the changelog in the release commit. After checks pass, publish its exact `vMAJOR.MINOR.PATCH` tag, then update onboarding defaults. Never move a published exact tag. Existing services upgrade explicitly.

For help, contact `#icicle-service-support` or open an [issue](https://github.com/icicle-ai/cicd-templates/issues).
