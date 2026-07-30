# ╭─────────────────────────────────────────────────────────────────────────╮
# │ ICICLE Official Dockerfile for static frontend services                 │
# │   (React, Vue, Svelte, or any tool that builds to static files)         │
# │                          - managed by #icicle-service-leads             │
# │                                                                         │
# │ ⚠  THIS FILE IS THE SOURCE OF TRUTH FOR CI BUILDS.                      │
# │    Do NOT fork, rename, or commit a modified copy to your service repo. │
# │    CI will overwrite local changes at build time.                       │
# │                                                                         │
# │ WHAT THIS IS: Node builds your app in the build stage, then nginx       │
# │    serves the static output. NO Node process runs in production.        │
# │    If your frontend needs a live Node server (SSR — Next.js, Remix,     │
# │    Nuxt in server mode), use the `node-backend/` template instead.      │
# │                                                                         │
# │ YOUR REPO MUST CONTAIN:                                                 │
# │   - icicle-service.yaml  (ICICLE deploy config)                         │
# │   - package.json         (with a "build" script)                        │
# │   - package-lock.json    (run `npm install` to generate)                │
# │   - .dockerignore        (copy from icicle-ai/cicd-templates)           │
# │                                                                         │
# │ YOUR BUILD MUST OUTPUT TO ./dist  (Vite / Vue default).                 │
# │   Create React App outputs to ./build — set BUILD_PATH=dist in your     │
# │   build script, or see node-frontend/service-setup.md.                  │
# │                                                                         │
# │ LOCAL TEST:                                                             │
# │   docker build \                                                        │
# │     --build-arg NODE_VERSION=22.11 \                                    │
# │     --build-arg BUILD_APT_PACKAGES="" \                                 │
# │     --build-arg RUNTIME_APT_PACKAGES="" \                               │
# │     --build-arg BUILD_SHA=local-test \                                  │
# │     -t my-frontend:test .                                               │
# │   docker run --rm -p 8000:8000 my-frontend:test                         │
# │                                                                         │
# │ NEED SOMETHING THIS TEMPLATE DOESN'T SUPPORT?                           │
# │   Open an issue in github.com/icicle-ai/cicd-templates and ping         │
# │    #icicle-service-support on slack. Do not patch locally.              │
# ╰─────────────────────────────────────────────────────────────────────────╯

# ╭─────────────╮
# │ Build-Stage │
# ╰─────────────╯
# Identical Node install to the `node-backend/` template: Node from the official dist
# tarball onto the same digest-pinned Debian base, verified against SHASUMS.
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS build-stage

ARG BUILD_APT_PACKAGES=""
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      xz-utils \
      $BUILD_APT_PACKAGES; \
    rm -rf /var/lib/apt/lists/*

# Node version in MAJOR.MINOR form, from icicle-service.yaml runtime-version.
# This is the version used to BUILD your frontend — it does not run in production.
ARG NODE_VERSION=22.11
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "ERROR: unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    full_version="$( \
      curl -fsSL https://nodejs.org/dist/index.json \
      | grep -o "\"version\":\"v${NODE_VERSION}\.[0-9]*\"" \
      | head -n1 \
      | cut -d'"' -f4 \
    )"; \
    if [ -z "$full_version" ]; then \
      echo "ERROR: no Node release found for runtime-version ${NODE_VERSION}." >&2; \
      exit 1; \
    fi; \
    tarball="node-${full_version}-linux-${node_arch}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/${full_version}/${tarball}"; \
    curl -fsSL "https://nodejs.org/dist/${full_version}/SHASUMS256.txt" -o SHASUMS256.txt; \
    grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c -; \
    mkdir -p /node; \
    tar -xJf "$tarball" -C /node --strip-components=1 --no-same-owner; \
    rm -f "$tarball" SHASUMS256.txt; \
    /node/bin/node --version

ENV PATH="/node/bin:$PATH" \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    CI=true

WORKDIR /app

RUN --mount=type=bind,source=.,target=/src,rw \
    test -f /src/icicle-service.yaml || (echo "ERROR: icicle-service.yaml is missing. See template header." && exit 1) \
    && test -f /src/package.json || (echo "ERROR: package.json is missing. See template header." && exit 1) \
    && test -f /src/package-lock.json || (echo "ERROR: package-lock.json is missing. Run 'npm install' in your repo." && exit 1) \
    && node -e 'process.exit((require("/src/package.json").scripts||{}).build?0:1)' \
       || (echo "ERROR: package.json has no \"build\" script. A static frontend must define one." && exit 1)

RUN --mount=type=cache,target=/root/.npm \
    --mount=type=bind,source=package.json,target=package.json \
    --mount=type=bind,source=package-lock.json,target=package-lock.json \
    npm ci --include=dev

COPY . /app

# Build the frontend. Output must land in /app/dist.
RUN npm run build

# Fail with a clear message if the build didn't produce dist/, rather than
# shipping an empty nginx that 404s every request.
RUN test -d /app/dist && [ -n "$(ls -A /app/dist)" ] \
    || (echo "ERROR: build produced no /app/dist directory (or it is empty)." \
        && echo "       Your build must output to dist/. CRA users: set BUILD_PATH=dist." \
        && echo "       See node-frontend/service-setup.md." && exit 1)


# ╭───────────╮
# │ Run-Stage │
# ╰───────────╯
# nginx serves the static build. No Node here. Same digest-pinned base as the
# other ICICLE templates, with nginx installed from Debian.
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS run-stage

ARG RUNTIME_APT_PACKAGES=""
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      nginx \
      ca-certificates \
      $RUNTIME_APT_PACKAGES; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 icicle \
    && useradd --system --gid 999 --uid 999 --create-home icicle

# nginx normally starts as root and drops to a worker user, and writes its pid
# and temp files under /run and /var/lib/nginx. We run the whole thing as the
# unprivileged icicle user instead (uid 999, as in the other templates), so those paths
# must be writable by that user. The config points them all at /tmp; here we
# just make sure the directories nginx expects exist and are owned correctly.
RUN set -eux; \
    mkdir -p /var/log/nginx /var/lib/nginx /tmp/nginx; \
    chown -R icicle:icicle /var/log/nginx /var/lib/nginx /tmp/nginx; \
    # nginx logs to files by default; symlink to stdout/stderr so logs reach
    # the Tapis log stream instead of vanishing inside the container.
    ln -sf /dev/stdout /var/log/nginx/access.log; \
    ln -sf /dev/stderr /var/log/nginx/error.log

# The ICICLE nginx config: listens on 8000, SPA fallback routing, a /healthcheck
# location, sensible cache headers. This file IS part of the template — unlike
# the Dockerfile it's meant to be reasonable for every frontend as-is.
# CI fetches it next to the Dockerfile into the build-context root (see the
# reusable workflow's "Fetch official Dockerfile" step), so it's referenced
# here as a plain context-root path.
COPY nginx.conf /etc/nginx/nginx.conf

# The built static site.
COPY --from=build-stage --chown=icicle:icicle /app/dist /app/site

# Build metadata. Written to a file the /healthcheck endpoint can serve, since there
# is no application process to read an env var.
ARG BUILD_SHA="unknown"
RUN printf '%s' "${BUILD_SHA}" > /app/site/.build_sha \
    && chown icicle:icicle /app/site/.build_sha

USER icicle
WORKDIR /app

EXPOSE 8000

# nginx handles SIGTERM/SIGQUIT correctly as PID 1. `daemon off` keeps it in
# the foreground so it IS PID 1 rather than forking away.
ENTRYPOINT ["nginx", "-g", "daemon off;"]
