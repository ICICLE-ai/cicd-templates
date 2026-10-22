# Deployment

Services run as unprivileged `icicle` (UID 999). Frontend Nginx listens on **`0.0.0.0:5000`** by default; runtime `PORT` overrides it. Set the Tapis pod's networking target port to match. Existing frontend pods targeting 8000 can keep that port by setting `PORT=8000` before upgrading.

Backend ports are application-controlled; the Python and Node examples use 8000. Setting `PORT` changes a backend's port only if its application or startup script reads it. Backends start with a POSIX shell script ending in `exec`; applications handle shutdown signals. Keep the frontend image's shared entrypoint and allow time for graceful shutdown.

New frontend API clients **must use relative `/api/...` requests**. Backends must serve the same full paths. Frontend proxying is optional for compatibility: set runtime `BACKEND_URL` to enable it, or retain existing ingress routing. See the [frontend guide](../node-frontend/service-setup.md). CI validates configuration, not application route implementations.

| Runtime | Health endpoint | Required response |
|---|---|---|
| Python / Node backend | `/health` | HTTP 200 JSON with `status: "ok"` and `build_sha` equal to `BUILD_SHA`. |
| Static frontend | `/healthcheck` | HTTP 200 with the bare build SHA; provided by Nginx. |

CI also supplies `BUILD_VERSION` to backends. Prefer it for health version metadata, with package metadata as a local fallback. Health routes stay outside `/api`.

CI builds and pushes the image tagged with `service-version`, then deploys the exact digest returned by that build. For `service-name: myservice` and `service-version: 1.4.2`, that image is `ghcr.io/icicle-ai/myservice:1.4.2`. This application version is independent of the shared template release.

The Python deployment helper sends `PUT /v3/pods/myservice` with `{"image":"ghcr.io/icicle-ai/myservice@sha256:<digest>"}` as JSON, then calls `GET /v3/pods/myservice/restart`. It verifies that a new instance serves the expected build SHA; CI records the image digest in the Actions summary. If the pod already has the exact image-reference string configured, the helper logs that it is skipping PUT. It still reads back the configuration, requests a restart, and verifies the new instance and build SHA. The pod must already exist; its environment, networking, volumes, and resources remain platform-managed.

Bump `service-version` for each new deployable build. Deployment by digest avoids stale tag caching, while distinct version tags keep releases easy to identify. CI does not bump versions or prevent tag reuse.

The Actions runner must reach the health endpoint. Health redirects are rejected; an optional `HEALTHCHECK_TOKEN` goes only to that endpoint, never in the Tapis API request. Assign one repository to each pod: deployment serialization is scoped to the repository.

Safe GET requests retry transient failures; image updates and restarts are never blindly retried. Automatic push deployments check the branch head immediately before rollout and skip stale builds. An explicit `workflow_dispatch` run can deploy an older selected ref for rollback.

A failed rollout fails CI and leaves the requested image configured. Inspect the reported previous image, pod status, and logs before retrying or rolling back. CI does not automatically roll back.

## Request logs and debug mode

The Python deployment helper logs each outgoing HTTP method and URL, HTTP status,
response-header timing, and connection failures, including Tapis updates/restarts,
health checks, and the GitHub branch freshness check. Request and response headers
and bodies are omitted, including in debug mode, to keep tokens and pod secrets
out of logs.

To debug a GitHub Actions run:

- Open the run under **Actions**, choose **Re-run jobs**, and select **Enable debug logging**.
- Or use `gh run rerun RUN_ID --debug` (add `--failed` to rerun only failed jobs).
- For ongoing step diagnostics, set the repository Actions variable or secret
  `ACTIONS_STEP_DEBUG` to `true` in the repository running the workflow.
  `ACTIONS_RUNNER_DEBUG=true` separately enables runner diagnostic files.

See GitHub's [debug logging documentation](https://docs.github.com/en/actions/how-tos/monitor-workflows/enable-debug-logging)
and [rerun instructions](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).
The scripts automatically detect GitHub's `RUNNER_DEBUG=1` and also accept
`ACTIONS_STEP_DEBUG=true` when supplied in their environment. They print
`DEBUG MODE ENABLED` with the activation source. Deployment debug logs add request
timeouts, read retries, branch freshness results, and reasons rollout checks are
still waiting. Debug mode performs the same deployment operations as a normal run.

All three Python tools also accept `--debug` locally:

```sh
python scripts/validate_config.py icicle-service.yaml --debug
python scripts/render_wiki.py /tmp/wiki-preview --debug
# Add --debug to your usual deploy_tapis.py invocation (requires TAPIS_TOKEN).
```

Logs go to stderr so `validate_config.py --json` remains valid JSON on stdout.
Consumers pinned to a template release need a release containing these changes
before the new script logging is available in their workflow.
