# cicd-templates

> Canonical Dockerfiles, configs, and CI workflow for ICICLE services.

This repo is the **source of truth** for ICICLE service build templates and deployment workflow. Service repos call the shared workflow to validate configuration, build and push an image, update an existing Tapis pod, and verify that the expected build is serving.

If you're a researcher onboarding a new service, jump to the per-runtime setup guide:

- **Python** → [`python/service-setup.md`](./python/service-setup.md)
- **Node backend** (API servers) → [`node-backend/service-setup.md`](./node-backend/service-setup.md)
- **Node frontend** (React/Vue/Svelte SPAs, built by Node and served by nginx) → [`node-frontend/service-setup.md`](./node-frontend/service-setup.md)

---

## What this repo provides

| Path | Purpose | Who uses it |
|---|---|---|
| `.github/workflows/deploy-service.yaml` | Shared build, image update, restart, and rollout verification. | CI (every service repo) |
| `scripts/` | Typed config validation and Tapis deployment helpers used by the workflow. | CI and maintainers |
| `VERSION` | Shared release version for all Dockerfiles, runtime templates, and the workflow. | CI and maintainers |
| `CHANGELOG.md` | Template release changes and migration notes. | Maintainers and service authors |
| `schema/` | Versioned service configuration contracts; CI supports schemas 1 and 2. | CI and service authors |
| `python/Dockerfile` | Official Dockerfile for Python services using uv. Fetched at build time. | CI |
| `python/.dockerignore` | Starter `.dockerignore` for Python services. Researchers copy this once. | Researchers |
| `python/entrypoint.sh` | Starter entrypoint script for Python services. Researchers copy and customize. | Researchers |
| `python/service-setup.md` | Step-by-step onboarding guide for Python service authors. | Researchers |
| `node-backend/Dockerfile` | Official Dockerfile for backend Node services. Fetched at build time. | CI |
| `node-backend/.dockerignore` | Starter `.dockerignore` for Node services. Researchers copy this once. | Researchers |
| `node-backend/entrypoint.sh` | Starter entrypoint script for Node services. Researchers copy and customize. | Researchers |
| `node-backend/service-setup.md` | Onboarding guide for backend Node service authors. | Researchers |
| `node-frontend/Dockerfile` | Official Dockerfile for static frontends (Node builds, nginx serves). Fetched at build time. | CI |
| `node-frontend/nginx.conf` | Shared nginx template: SPA fallback, cache headers, `/healthcheck`, and optional API include. Fetched at build time. | CI |
| `node-frontend/nginx-api.conf.template` | Optional `/api/` proxy configured with `BACKEND_URL` at container startup. Fetched at build time. | CI |
| `node-frontend/entrypoint.sh` | Shared frontend startup: render writable configuration, validate it, and start nginx. Fetched as `nginx-entrypoint.sh`. | CI |
| `node-frontend/.dockerignore` | Starter `.dockerignore` for frontend services. Researchers copy this once. | Researchers |
| `node-frontend/service-setup.md` | Onboarding guide for frontend authors. | Researchers |
| `icicle-service.yaml` | Empty starter config. Researchers copy and fill in. | Researchers |
| `icicle-service-example.yaml` | Annotated example showing every field with comments. | Researchers (reference) |
| `deploy.yaml` | Example workflow for a service repo. Researchers copy this into their `.github/workflows/`. | Researchers |

---

## How a service uses this repo

The flow, from researcher push to deployed pod:

```
┌──────────────────────────────────────────────────────────┐
│ Researcher's service repo                                │
│   icicle-service.yaml      ← service config              │
│   deps manifest            ← pyproject.toml, package.json │
│   src/                     ← code                        │
│   .github/workflows/       ← calls the shared workflow   │
│     deploy.yaml            │  the reusable workflow here │
└─────────────────────┬────────────────────────────────────┘
                      │  push to main
                      ▼
┌──────────────────────────────────────────────────────────┐
│ cicd-templates (THIS REPO)                               │
│   .github/workflows/deploy-service.yaml                  │
│     1. Parse + validate icicle-service.yaml              │
│     2. Fetch Dockerfile + runtime templates              │
│     3. Build + push to ghcr.io                           │
│     4. Update pod image, restart, verify build SHA       │
└─────────────────────┬────────────────────────────────────┘
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
   ┌─────────┐                 ┌─────────┐
   │  GHCR   │                 │  Tapis  │
   └─────────┘                 └─────────┘
```

Researchers use `deploy.yaml` and fill in `icicle-service.yaml`. A platform operator provisions the Tapis pod, networking, runtime environment, and registry access. CI updates the pod's image for each deployment and verifies the rollout.

---

## Shared service requirements

These are the common interfaces for new services. Runtime guides provide language-specific examples; the backend frameworks shown are examples, not template requirements.

| Requirement | Standard | Enforcement |
|---|---|---|
| Service and pod identity | New services use schema 2 and set `service-name` equal to `pod-name`: 4–64 lowercase ASCII letters/digits, starting with a letter. Example: both `accountapi01`. The name becomes the registry image basename and identifies the existing Tapis pod. | CI validates `^[a-z][a-z0-9]{3,63}$` and equality for schema 2; Tapis checks existence, reserved names, and access. Schema 1 retains its existing service-name rules. |
| Runtime | `python`, `node-backend`, or `node-frontend`; quote `runtime-version` as `MAJOR.MINOR`. | Typed configuration validation checks supported values and version format. |
| Release metadata | `service-version` is the deployed version. `project-name` and `service-type` identify ownership/category. | CI sets `BUILD_VERSION` for backends and records project/type as image labels. Package versions may differ. |
| Browser API requests | New frontend application API requests **must** use same-origin relative `/api/...` paths. Do not embed backend hosts in source or browser build variables. | Required application convention; CI does not inspect application routes or JavaScript calls. |
| Backend API routes | APIs used by frontends must serve the full `/api/...` route themselves; the shared proxy preserves that prefix and query string. | Backend implementation and integration tests. |
| Network listener | Services listen on `0.0.0.0:8000` by default; platform networking must match the configured port. | Application and deployment configuration. |
| Health | Backends expose `/health` with JSON `status: "ok"` and `build_sha`; static frontends expose `/healthcheck` with the bare build SHA. Keep these operational paths outside `/api`. | CI compares the response with the commit it built. Additional service/version/timestamp fields aid diagnosis. |
| Process startup | Run as unprivileged `icicle`; backend entrypoints use POSIX shell and end with `exec`. Keep the shared frontend entrypoint. | Image configuration and runtime tests; backend signal handling remains the application's responsibility. |

The lowercase pod naming restriction comes from [Tapis](https://tapis.readthedocs.io/en/latest/technical/pods.html#pods-quickstart). ICICLE's minimum is four characters; the platform also caps IDs at 64. Here “container name” means the image basename, such as `accountapi01` in `ghcr.io/icicle-ai/accountapi01:0.1.0`. Tapis controls its internal container IDs. Package names, repository names, and `project-name` do not need to match the pod.

The frontend proxy stays optional **for backward compatibility** and sites with no API. New applications that call a backend must follow the `/api/...` convention and configure runtime `BACKEND_URL` when using the shared proxy. Existing ingress routing can supply the same public `/api/...` interface without enabling the container proxy. The API routing convention does not rename backend health endpoints.

---

## Configuration and compatibility

**New services must use `schema-version: 2`**, as the starter does. Schema 2 requires `service-name` and `pod-name` to match. Existing services can continue using schema 1, including a hyphenated service name that differs from the pod name. Required field names remain unchanged. CI validates YAML types before using values: names and versions are strings, `schema-version` is an integer, and package lists contain Debian package names rather than shell commands. Quote `runtime-version` to preserve values such as `"3.10"`. Legacy unquoted decimal versions are accepted with their original spelling and a warning. Unknown extension fields produce a warning so existing service-specific metadata remains usable. CI enforces the selected schema; it cannot identify a newly created service that deliberately selects schema 1. New-service onboarding review must require schema 2.

To migrate an existing service, keep its provisioned `pod-name`, change `service-name` to that name, and set `schema-version: 2`. Coordinate registry package access and the Tapis image allowlist before deploying the newly named image. Update image references in scripts and documentation. The workflow does not rename or create pods, and migration is unnecessary for services remaining on schema 1.

Optional settings have defaults and require no changes to existing configurations:

| Setting | Default | Purpose |
|---|---|---|
| `health-url` | Discover from pod HTTP networking | Exact HTTP(S) health endpoint reachable by the Actions runner. No credentials, query, or fragment. |
| `rollout-timeout` | `300` seconds | Integer from 30 to 1800; bounds rollout and health verification. |
| `include-dev-dependencies` | `true` when omitted | Python compatibility setting. New-service starters explicitly set `false` to omit the dev group from the final image. |

For Python, set `include-dev-dependencies: false` for a production environment without the dev group. Existing configurations that omit it keep their previous dependency behavior; explicit `true` does the same. Runtime imports belong in project dependencies before switching an existing service to `false`.

For example, a deployment with a custom health route can add:

```yaml
health-url: "https://service.example.org/health"
rollout-timeout: 600
```

See the [annotated configuration](./icicle-service-example.yaml) for the complete fields. Framework, module format, source layout, and Git staging commands are application choices. The shared interfaces above and each runtime's actual build inputs are what the templates require.

---

## Deployment and verification

The reusable workflow requires `TAPIS_TOKEN`, `REGISTRY_USERNAME`, and `REGISTRY_PASSWORD`, normally supplied through organization secrets and forwarded by the caller. Repository visibility itself is not a template requirement; the repository must be authorized to access those credentials.

CI pushes the familiar `<service-name>:<service-version>` tag and a unique build tag containing the commit, workflow run ID, and attempt. It deploys the unique tag so repeated releases do not accidentally select an older image with the same version tag. The build digest is recorded in workflow output; a unique tag is not a registry-enforced immutability guarantee.

The deployment helper updates only the existing pod's `image`, then requests a restart. Environment variables, networking, mounts, and other pod settings remain platform-managed. The workflow waits until the configured image matches, the pod is `AVAILABLE`, and it has evidence of a new start or restart transition. It then checks the application's health response against the full commit SHA:

- **Python/Node backend:** HTTP 200 with JSON `status: "ok"` and the expected `build_sha` from `/health`.
- **Static frontend:** HTTP 200 with the bare SHA from `/healthcheck`.

Assign one service repository as the deployment owner of each pod. CI serializes deployments within that repository; GitHub concurrency does not coordinate different repositories targeting the same pod. The helper fails if it observes another image replacing the one it requested.

Backends receive `BUILD_SHA` and `BUILD_VERSION` from CI. Prefer these values in health responses, using package metadata as a local-development fallback. Existing healthy applications that report the correct `build_sha` continue to work; rollout verification does not require their package version to equal `service-version`.

By default, CI derives the health URL from the pod's default HTTP networking endpoint, or its sole HTTP endpoint. Set `health-url` when the endpoint is ambiguous or uses a custom route. It must be reachable from the Actions runner. For a protected health endpoint, configure and forward the optional `HEALTHCHECK_TOKEN` secret; CI sends it as a Bearer token only to that health URL. Tapis credentials are used only for Tapis API requests, and health redirects are not followed.

Update/restart failures, a failed rollout, stale health SHA, or the verification timeout fail the workflow. A failed verification leaves the requested image configured; inspect the failure and the reported previous image before retrying or choosing a rollback. Inspect workflow diagnostics and the Tapis pod status and logs for diagnosis (`GET /v3/pods/<pod-name>` and `GET /v3/pods/<pod-name>/logs`). These checks verify the deployed process and build identity; use application tests for API correctness and downstream dependencies. Provision the pod and registry access before the first deployment, and allow enough shutdown grace for active requests.

---

## Versioning

All three Dockerfiles, shared startup/nginx files, validation helpers, and the deployment workflow are versioned together. [`VERSION`](./VERSION) declares the template release version (`2.0.0` for this change). Release tags use `vMAJOR.MINOR.PATCH`; publish each exact tag once and do not move it. A Dockerfile does not have a separate version selector that can drift from its startup files or workflow.

The current starter `deploy.yaml` follows `@main`, so shared changes are picked up on the next service build. For production, replace it with an **existing reviewed exact release tag or commit**:

```yaml
uses: icicle-ai/cicd-templates/.github/workflows/deploy-service.yaml@<reviewed-tag-or-commit>
```

The reusable workflow fetches Dockerfiles and runtime templates from its own repository and immutable commit (`job.workflow_repository` and `job.workflow_sha`). Use that same ref for manual template downloads. Declaring `2.0.0` in `VERSION` does not publish `v2.0.0`; verify the tag exists before using it. The published `v1` predates the changes documented here.

These versions have separate purposes:

| Version | Controls |
|---|---|
| Template release / workflow `@ref` | Dockerfiles, startup files, workflow, and helpers used to build and deploy. CI records `io.icicle.template.version` and `io.icicle.template.revision` on the resulting image. The revision identifies the exact source commit. |
| `schema-version` | Validation rules for `icicle-service.yaml`; this template release supports schemas 1 and 2. |
| `service-version` | Your application's release tag and backend `BUILD_VERSION`. |
| `runtime-version` | Python or Node version used by the selected Dockerfile. |

All Dockerfiles accept `TEMPLATE_VERSION` and `TEMPLATE_REVISION` build arguments for those image labels. CI supplies them from the selected template source; manual builds default to `development` and `unknown` unless you pass them explicitly.

**Upgrading services:** update the workflow's `@ref`, use the same ref for manual downloads, review the [migration notes](./CHANGELOG.md), and build a new image. Updating the template release does not force an existing service to migrate to schema 2. Runtime-only changes such as `BACKEND_URL` need no rebuild once the image contains startup proxy support.

---

## For maintainers

### Making changes

Anything in this repo affects every ICICLE service that pins to the affected tag. Process:

1. Open a PR against `main`. CI runs typed configuration, deployment-controller, nginx integration, and image smoke tests for relevant changes.
2. Test against a real service by temporarily pointing its `deploy.yaml` at your branch (`@your-branch-name`).
3. Merge the PR.
4. Update `VERSION` and **publish the matching exact release tag** after CI passes. Never move an exact release tag. Document supported runtimes, schema versions, and migrations. Services following `@main` see merged changes on their next build; pinned services upgrade explicitly.
5. Announce in `#icicle-service-support` with what changed and migration notes if any.

### What can break a service

In rough order of impact:

- **Schema changes to `icicle-service.yaml`** — adding required fields breaks every existing service. Add optional fields with defaults, or introduce a versioned migration that keeps existing supported configurations working.
- **Removing or renaming Dockerfile build args** — services pinning to the new tag will fail at build time. Add new args, deprecate old ones over a release cycle.
- **Changing the validation regex** — services with previously-valid configs will start failing. Test against existing service repos before merging.
- **Bumping base image** — usually safe, but a major Debian/Python/Node version bump can break services that depend on specific system libraries. Test broadly.

### Adding a new runtime

To add support for a new runtime (Go, Rust, etc.):

1. Create `<runtime>/Dockerfile` following the patterns in `python/Dockerfile`. It should accept `<RUNTIME>_VERSION`, `BUILD_APT_PACKAGES`, `RUNTIME_APT_PACKAGES`, `BUILD_SHA`, and `BUILD_VERSION` build args, and run as the non-root `icicle` user (uid/gid 999).
2. Create `<runtime>/.dockerignore` and, if startup initialization is needed, `<runtime>/entrypoint.sh`. The frontend template has a shared entrypoint that renders nginx configuration before replacing itself with nginx.
3. Add a `<runtime>)` branch to the version `case` in the `Set up Buildtime Args` step, emitting the build arg your Dockerfile expects. **Don't skip this** — a Dockerfile that reads a `*_VERSION` arg the workflow never passes falls back to its `ARG` default and silently builds the wrong version.
4. Add the runtime to the typed configuration schema and deployment health handling.
5. If the runtime's Dockerfile `COPY`s any file *other* than what the service repo provides (e.g. `node-frontend` ships an `nginx.conf`), copy that file into the build context in the `Copy official build sources` step — the build context is the service repo, so template-owned files must be pulled in explicitly.
6. Write `<runtime>/service-setup.md` for researchers.
7. Tag a new version.

---

## Frontend runtime API proxy

New frontend API clients must use the relative `/api/...` convention above. To route those requests through the shared proxy, set `BACKEND_URL` when starting a container; the same image works across backend environments:

```bash
docker run --rm --network app-network -p 8000:8000 \
  -e BACKEND_URL=http://backend:3000 accountfrontend01:tag
```

The `backend` container must be reachable on that Docker network. Frontend code calls relative paths such as `apiFetch("/api/account/session")`; nginx forwards the complete path and query string to the configured backend. Leave `BACKEND_URL` unset or empty to retain the existing static/SPA behavior. Use an HTTP or HTTPS origin, with no path, credentials, query, or fragment; one trailing slash is accepted.

For Tapis, set `BACKEND_URL` in the existing pod's `environment_variables`, preserve its other environment settings, and restart the pod. Use a backend address reachable from that pod, typically its platform-provided HTTPS networking URL. CI preserves runtime environment variables when updating the image; it does not configure `BACKEND_URL` for you. Adopting this startup support requires one image rebuild; later backend changes only require updating the runtime setting and restarting the same image.

The authoritative files are in `node-frontend/` in this repository. CI overwrites fetched copies in service repositories. See the [frontend setup guide](./node-frontend/service-setup.md#step-5--config-and-api-urls) for networking, HTTPS, and local testing details.

---

## Testing the shared templates

Install the Python helper dependencies and run the unit and local nginx integration suite:

```bash
python3 -m venv /tmp/icicle-template-tests
. /tmp/icicle-template-tests/bin/activate
python -m pip install -r scripts/requirements.txt
python -m unittest discover -s tests -v
```

Use Python 3.9+, nginx with HTTP SSL support, GNU `envsubst` (`gettext-base` on Debian), and `openssl`. `NGINX_BINARY=/absolute/path/nginx` selects a local nginx build. The suite validates typed config and deployment-controller behavior, and exercises the shared entrypoint with real nginx and local HTTP/HTTPS backends. Docker tests are skipped unless explicitly enabled.

With a running Docker daemon, BuildKit, and network access for image/package downloads, run the real image smoke tests:

```bash
REQUIRE_DOCKER=1 python tests/test_images.py -v
```

These build all three Dockerfiles from minimal service fixtures, including Python's production/dev compatibility variants, and check startup, health metadata, non-root execution, and frontend-to-backend networking. The [test workflow](./.github/workflows/test-templates.yaml) runs these checks in CI. Local fixtures and mocked Tapis responses do not verify a live deployment; use a provisioned test pod before adopting a new template release.

---

## Need something the templates don't support?

Propose shared build or startup changes here, or open an issue and contact `#icicle-service-support`. CI replaces downloaded template copies, so service-local edits to those files will not persist. Keep application initialization in service-owned backend entrypoints and runtime settings.

---

## Contact

- Slack: `#icicle-service-support`
- Issues: [icicle-ai/cicd-templates/issues](https://github.com/icicle-ai/cicd-templates/issues)
