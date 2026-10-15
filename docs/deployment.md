# Deployment

Services run as unprivileged `icicle` (UID/GID 999). New provisioned pods set runtime **`PORT=5000`** by default; applications must read `PORT` and bind to `0.0.0.0`. An explicit `PORT` overrides the pod's networking port too. Existing services and frontends without `PORT` retain port `8000`. Backends start with a POSIX shell script ending in `exec`; applications handle shutdown signals. Keep the frontend image's shared entrypoint and allow time for graceful shutdown.

New frontend API clients **must use relative `/api/...` requests**. Backends must serve the same full paths. Frontend proxying is optional for compatibility: set runtime `BACKEND_URL` to enable it, or retain existing ingress routing. See the [frontend guide](../node-frontend/service-setup.md). CI validates configuration, not application route implementations.

| Runtime | Health endpoint | Required response |
|---|---|---|
| Python / Node backend | `/health` | HTTP 200 JSON with `status: "ok"` and `build_sha` equal to `BUILD_SHA`. |
| Static frontend | `/healthcheck` | HTTP 200 with the bare build SHA; provided by Nginx. |

CI also supplies `BUILD_VERSION` to backends. Prefer it for health version metadata, with package metadata as a local fallback. Health routes stay outside `/api`.

CI builds and pushes the application version tag plus a unique build tag. It updates only the pod's image, restarts it, and checks for a new instance serving the expected SHA. It records the image digest; unique tags are not registry-enforced immutability. Pod environment, networking, volumes, and resources remain platform-managed.

For optional creation and manual start/stop/restart, use the platform [pods and volumes workflows](provisioning.md). They keep developers at Tapis `USER` and require a separate privileged automation identity. Do not run platform operations concurrently with a service deployment for the same pod.

The Actions runner must reach the health endpoint. Health redirects are rejected; an optional `HEALTHCHECK_TOKEN` goes only to that endpoint, never in the Tapis API request. Assign one repository to each pod: deployment serialization is scoped to the repository.

Safe GET requests retry transient failures; image updates and restarts are never blindly retried. Automatic push deployments check the branch head immediately before rollout and skip stale builds. An explicit `workflow_dispatch` run can deploy an older selected ref for rollback.

A failed rollout fails CI and leaves the requested image configured. Inspect the reported previous image, pod status, and logs before retrying or rolling back. CI does not automatically roll back.
