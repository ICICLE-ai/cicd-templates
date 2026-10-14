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
# │   - pyproject.toml       (Python deps and project configuration)        │
# │   - uv.lock              (run `uv lock` to generate)                    │
# │   - entrypoint.sh        (your service start script, must use `exec`)   │
# │   - .dockerignore        (copy from icicle-ai/cicd-templates)           │
# │                                                                         │
# │ LOCAL TEST:                                                             │
# │   docker build \                                                        │
# │     --build-arg PYTHON_VERSION=3.13.16 \                                   │
# │     --build-arg BUILD_APT_PACKAGES="" \                                 │
# │     --build-arg RUNTIME_APT_PACKAGES="" \                               │
# │     -t my-service:test .                                                │
# │   docker run --rm -p 8000:8000 my-service:test                          │
# │                                                                         │
# │ CI passes these build args from icicle-service.yaml through the         │
# │ shared GitHub Actions workflow.                                        │
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

COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /uvx /bin/

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

# Python version. Populated from icicle-service.yaml runtime-version.
ARG PYTHON_VERSION=3.14.8
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install ${PYTHON_VERSION}

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --python "$PYTHON_VERSION"

COPY . /app

# Check source-only requirements after the dependency cache boundary.
RUN test -f icicle-service.yaml || { echo "ERROR: icicle-service.yaml is missing" >&2; exit 1; }; \
    test -f entrypoint.sh || { echo "ERROR: entrypoint.sh is missing" >&2; exit 1; }

# Preserve the historical dependency set when this argument is omitted.
# New service configs explicitly pass false to exclude the dev group from the
# production environment after build tools have been used above.
ARG INCLUDE_DEV_DEPENDENCIES=true
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; \
    case "$INCLUDE_DEV_DEPENDENCIES" in \
      false) set -- --no-dev ;; \
      true) set -- ;; \
      *) echo "ERROR: INCLUDE_DEV_DEPENDENCIES must be true or false" >&2; exit 1 ;; \
    esac; \
    uv sync --locked --no-editable --python "$PYTHON_VERSION" "$@"


# ╭───────────╮
# │ Run-Stage │
# ╰───────────╯
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS run-stage

# Shared-template provenance is independent of the application's build version.
ARG TEMPLATE_VERSION="development"
ARG TEMPLATE_REVISION="unknown"
LABEL io.icicle.template.version=${TEMPLATE_VERSION} \
    io.icicle.template.revision=${TEMPLATE_REVISION}

# Runtime apt packages. Populated from icicle-service.yaml runtime-apt-packages.
ARG RUNTIME_APT_PACKAGES=""
# Managed Python uses the system trust store for outbound HTTPS.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates $RUNTIME_APT_PACKAGES; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 icicle \
    && useradd --system --gid 999 --uid 999 --create-home icicle

COPY --from=build-stage /python /python
COPY --from=build-stage --chown=icicle:icicle /app /app

# Build metadata — populated by CI for the /health endpoint.
ARG BUILD_SHA="unknown"
ARG BUILD_VERSION=""
ENV BUILD_SHA=${BUILD_SHA} \
    BUILD_VERSION=${BUILD_VERSION}

ENV PATH="/app/.venv/bin:$PATH" \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

USER icicle
WORKDIR /app

ENTRYPOINT ["sh", "entrypoint.sh"]
