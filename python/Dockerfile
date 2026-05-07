# ╭─────────────────────────────────────────────────────────────────────────╮
# │ ICICLE Official Dockerfile for Python-based services                    │
# │                          - managed by #icicle-service-leads             │
# │                                                                         │
# │ ⚠  THIS FILE IS THE SOURCE OF TRUTH FOR CI BUILDS.                      │
# │    Do NOT fork, rename, or commit a modified copy to your service repo. │
# │    CI will overwrite local changes at build time.                       │
# │                                                                         │
# │ PURPOSE: Local testing only. Use this to verify your service builds and │
# │          runs the same way it will in CI before you push.               │
# │                                                                         │
# │ YOUR REPO MUST CONTAIN:                                                 │
# │   - icicle-service.yaml  (ICICLE deploy config)                         │
# │   - pyproject.toml       (Python deps, uv_build backend)                │
# │   - uv.lock              (run `uv lock` to generate)                    │
# │   - entrypoint.sh        (your service start script, must use `exec`)   │
# │   - .dockerignore        (copy from icicle-ai/cicd-templates)           │
# │                                                                         │
# │ LOCAL TEST:                                                             │
# │   docker build \                                                        │
# │     --build-arg PYTHON_VERSION=3.13 \                                   │
# │     --build-arg BUILD_APT_PACKAGES="" \                                 │
# │     --build-arg RUNTIME_APT_PACKAGES="" \                               │
# │     -t my-service:test .                                                │
# │   docker run --rm -p 8000:8000 my-service:test                          │
# │                                                                         │
# │ CI passes these build args automatically from icicle-service.yaml       │
# │ via the Ansible deploy playbook.                                        │
# │                                                                         │
# │ NEED SOMETHING THIS TEMPLATE DOESN'T SUPPORT?                           │
# │   Open an issue in github.com/icicle-ai/cicd-templates and ping         │
# │    #icicle-service-support on slack. Do not patch locally.              │
# │                                                                         │
# │ Ref: https://github.com/astral-sh/uv-docker-example                     │
# ╰─────────────────────────────────────────────────────────────────────────╯

# ╭─────────────╮
# │ Build-Stage │
# ╰─────────────╯
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS build-stage

COPY --from=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed

# Build-stage apt packages. Populated from icicle-service.yaml build-apt-packages.
ARG BUILD_APT_PACKAGES=""
RUN if [ -n "$BUILD_APT_PACKAGES" ]; then \
      set -eux; \
      apt-get update && \
      apt-get install -y --no-install-recommends $BUILD_APT_PACKAGES && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Python version. Populated from icicle-service.yaml python-version.
ARG PYTHON_VERSION=3.14
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install ${PYTHON_VERSION}

WORKDIR /app

# Fail fast with a clear message if required files are missing.
RUN --mount=type=bind,source=.,target=/src,rw \
    test -f /src/icicle-service.yaml || (echo "ERROR: icicle-service.yaml is missing. See template header." && exit 1) \
    && test -f /src/pyproject.toml || (echo "ERROR: pyproject.toml is missing. See template header." && exit 1) \
    && test -f /src/uv.lock || (echo "ERROR: uv.lock is missing. Run 'uv lock' in your repo." && exit 1) \
    && test -f /src/entrypoint.sh || (echo "ERROR: entrypoint.sh is missing. See template header." && exit 1)

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


# ╭───────────╮
# │ Run-Stage │
# ╰───────────╯
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS run-stage

# Runtime apt packages. Populated from icicle-service.yaml runtime-apt-packages.
ARG RUNTIME_APT_PACKAGES=""
RUN if [ -n "$RUNTIME_APT_PACKAGES" ]; then \
      set -eux; \
      apt-get update && \
      apt-get install -y --no-install-recommends $RUNTIME_APT_PACKAGES && \
      rm -rf /var/lib/apt/lists/*; \
    fi

RUN groupadd --system --gid 999 icicle \
    && useradd --system --gid 999 --uid 999 --create-home icicle

COPY --from=build-stage /python /python
COPY --from=build-stage --chown=icicle:icicle /app /app

# Build metadata — populated by CI for the /health endpoint.
ARG BUILD_SHA="unknown"
ENV BUILD_SHA=${BUILD_SHA}

ENV PATH="/app/.venv/bin:$PATH"

USER icicle
WORKDIR /app

ENTRYPOINT ["sh", "entrypoint.sh"]
