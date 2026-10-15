# Deployment

Services run as unprivileged `icicle` (UID 999). Frontend Nginx listens on **`0.0.0.0:5000`** by default; runtime `PORT` overrides it. Set the Tapis pod's networking target port to match. Existing frontend pods targeting 8000 can keep that port by setting `PORT=8000` before upgrading.

Backend ports are application-controlled; the Python and Node examples use 8000. Setting `PORT` changes a backend's port only if its application or startup script reads it. Backends start with a POSIX shell script ending in `exec`; applications handle shutdown signals. Keep the frontend image's shared entrypoint and allow time for graceful shutdown.

New frontend API clients **must use relative `/api/...` requests**. Backends must serve the same full paths. Frontend proxying is optional for compatibility: set runtime `BACKEND_URL` to enable it, or retain existing ingress routing. See the [frontend guide](../node-frontend/service-setup.md). CI validates configuration, not application route implementations.

| Runtime | Health endpoint | Required response |
|---|---|---|
| Python / Node backend | `/health` | HTTP 200 JSON with `status: "ok"` and `build_sha` equal to `BUILD_SHA`. |
| Static frontend | `/healthcheck` | HTTP 200 with the bare build SHA; provided by Nginx. |

CI also supplies `BUILD_VERSION` to backends. Prefer it for health version metadata, with package metadata as a local fallback. Health routes stay outside `/api`.

CI builds and pushes the application version tag plus a unique build tag. It updates only the pod's image, restarts it, and checks for a new instance serving the expected SHA. It records the image digest; unique tags are not registry-enforced immutability. Pod environment, networking, volumes, and resources remain platform-managed.

The Actions runner must reach the health endpoint. Health redirects are rejected; an optional `HEALTHCHECK_TOKEN` goes only to that endpoint, never in the Tapis API request. Assign one repository to each pod: deployment serialization is scoped to the repository.

Safe GET requests retry transient failures; image updates and restarts are never blindly retried. Automatic push deployments check the branch head immediately before rollout and skip stale builds. An explicit `workflow_dispatch` run can deploy an older selected ref for rollback.

A failed rollout fails CI and leaves the requested image configured. Inspect the reported previous image, pod status, and logs before retrying or rolling back. CI does not automatically roll back.
