# Python services

Use this template for any Python web service managed with uv. Keep your existing framework, build backend, and module layout.

Follow the [shared quick start](../docs/quick-start.md#quick-start), then set `runtime-type: python` and a quoted `runtime-version`, such as `"3.13.16"`, in `icicle-service.yaml`.

## Required files

Your repository needs:

- `pyproject.toml` and a committed `uv.lock`.
- A service-owned `entrypoint.sh`.
- `.dockerignore`, copied from [this template](.dockerignore).
- The configuration and workflow from the shared quick start.

Use `uv add` when adding dependencies; run `uv lock` after editing project metadata or dependencies manually. CI runs `uv sync --locked`, so the lockfile must match the project.

The configured Python version takes precedence over `.python-version` inside the image. Your project's `requires-python` must allow it. Keep runtime imports in `[project.dependencies]`. New services should set `include-dev-dependencies: false` to exclude the `dev` group from the final environment; omitted or `true` preserves the legacy behavior. Build tools can still use the dev group during the build.

## Startup

Use a POSIX shell script that performs any initialization, then replaces itself with your server. For an existing Uvicorn application:

```sh
#!/bin/sh
set -eu
exec uvicorn your_package.main:app --host 0.0.0.0 --port "${PORT:-5000}"
```

Replace the module and server command with your application's own. For a server that starts itself, use `exec python -m your_package`. Make the script executable with `chmod +x entrypoint.sh`. The image invokes it with `sh` and puts the installed virtual environment on `PATH`.

New services must read runtime `PORT`, defaulting to `5000`, and listen on `0.0.0.0`; the pod's networking port must match. A server that starts itself can use `port = int(os.environ.get("PORT", "5000"))`. Existing services can retain port `8000`. The server runs as the unprivileged `icicle` user and must handle stop signals. See the shared [deployment requirements](../docs/deployment.md#deployment).

## API and health routes

APIs used by new frontends must expose `/api/...` routes. Frontend clients request those relative paths, and the shared frontend proxy preserves the entire path and query string. Keep `GET /health` outside that prefix.

Health must return HTTP 200 with JSON containing `"status": "ok"` and the image's full `BUILD_SHA` as `build_sha`. Keep it fast, avoid external dependency checks, and disable caching. CI uses it to verify the deployed build.

For an application already using FastAPI, the routes can look like this:

```python
import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI, Response

app = FastAPI()

try:
    app_version = os.getenv("BUILD_VERSION") or version("your-distribution")
except PackageNotFoundError:
    app_version = "unknown"


@app.get("/health")
def health(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "ok",
        "build_sha": os.getenv("BUILD_SHA", "unknown"),
        "version": app_version,
    }


@app.get("/api/example/items")
def example():
    return {"message": "Hello"}
```

Use your installed distribution name for the local version fallback. CI sets `BUILD_VERSION` from `service-version`; reporting it is useful but only `status` and `build_sha` are required for rollout verification. Other frameworks should return the same health contract. Add FastAPI and Uvicorn only if your application uses them.

## Test the image

Run from your service repository. Match the Python version and dependency setting to your [configuration](../docs/configuration.md#configuration):

```sh
TEMPLATES_REF=v2.2.0
dockerfile="$(mktemp)"
curl -fsSL "https://raw.githubusercontent.com/icicle-ai/cicd-templates/$TEMPLATES_REF/python/Dockerfile" -o "$dockerfile"
docker build -f "$dockerfile" \
  --build-arg PYTHON_VERSION=3.13.16 \
  --build-arg INCLUDE_DEV_DEPENDENCIES=false \
  --build-arg BUILD_SHA=local-test \
  --build-arg BUILD_VERSION=0.1.0 \
  -t pythonservice:test .
rm "$dockerfile"
docker run --rm -p 8000:5000 -e PORT=5000 pythonservice:test
```

In another terminal, run `curl -f http://localhost:8000/health` and request one of your `/api/...` routes. Health should report `build_sha: "local-test"`. Confirm imports work in this final image, where the project is installed without editable mode and dev dependencies may be absent.

If needed, pass `BUILD_APT_PACKAGES` and `RUNTIME_APT_PACKAGES` as space-separated Debian package names. Keep compilers and headers in build packages; put libraries needed by the running service in runtime packages.

CI fetches the authoritative Dockerfile from the same release as its workflow. See [versioning](../docs/versioning.md#versioning) when upgrading, and [deployment](../docs/deployment.md#deployment) for rollout checks and troubleshooting.
