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
# │     --build-arg NODE_VERSION=24.22.0 \                                    │
# │     --build-arg BUILD_APT_PACKAGES="" \                                 │
# │     --build-arg RUNTIME_APT_PACKAGES="" \                               │
# │     --build-arg BUILD_SHA=local-test \                                  │
# │     -t my-frontend:test .                                               │
# │   docker run --rm -p 5000:5000 my-frontend:test                         │
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
FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS build-stage

ARG BUILD_APT_PACKAGES=""
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      xz-utils \
      $BUILD_APT_PACKAGES; \
    rm -rf /var/lib/apt/lists/*

# Exact MAJOR.MINOR.PATCH pins the runtime; legacy MAJOR.MINOR keeps resolving
# the newest patch of that same minor line. Existing configs are not upgraded.
ARG NODE_VERSION=24.22.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "ERROR: unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    case "$NODE_VERSION" in \
      *[!0-9.]*|.*|*.) echo "ERROR: NODE_VERSION must be MAJOR.MINOR or MAJOR.MINOR.PATCH" >&2; exit 1 ;; \
    esac; \
    case "$(printf '%s' "$NODE_VERSION" | tr -cd '.' | wc -c | tr -d ' ')" in \
      1) full_version="$(curl -fsSL https://nodejs.org/dist/index.json \
           | grep -o "\"version\":\"v${NODE_VERSION}\\.[0-9]*\"" \
           | head -n1 | cut -d'"' -f4)" ;; \
      2) full_version="v${NODE_VERSION}" ;; \
      *) echo "ERROR: NODE_VERSION must be MAJOR.MINOR or MAJOR.MINOR.PATCH" >&2; exit 1 ;; \
    esac; \
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

# Include project npm configuration in the dependency cache key. The wildcard
# is optional; package.json and package-lock.json are always required.
COPY package.json package-lock.json .npmr[c] npm-shrinkwrap.jso[n] binding.gy[p] ./

# Retain dependency/native lifecycle scripts. Root lifecycle hooks, workspaces,
# and local dependencies need the complete repository, so install after COPY.
# Unusual dependency hooks that read application files can opt into full mode.
ARG NPM_INSTALL_MODE=auto
RUN --mount=type=cache,target=/root/.npm \
    set -eu; \
    case "$NPM_INSTALL_MODE" in auto|full) ;; \
      *) echo "ERROR: NPM_INSTALL_MODE must be auto or full" >&2; exit 1 ;; esac; \
    node -e ' \
      const fs = require("fs"), p = require("./package.json"), lock = require(fs.existsSync("npm-shrinkwrap.json") ? "./npm-shrinkwrap.json" : "./package-lock.json"); \
      const hooks = ["preinstall", "install", "postinstall", "prepublish", "preprepare", "prepare", "postprepare"]; \
      const local = value => typeof value === "string" && /^(file:|link:|workspace:|\.?\.?\/)/.test(value); \
      const deps = [p.dependencies, p.devDependencies, p.optionalDependencies]; \
      /* npm parses quoted/escaped/scoped INI keys; inspect its normalized output. */ \
      /* Keep output captured: config values may contain credentials. JSON hides auth keys. */ \
      const normalized = require("child_process").spawnSync("npm", ["config", "list"], {encoding: "utf8"}); \
      if (normalized.status !== 0) throw new Error("Unable to validate project npm configuration"); \
      const entries = normalized.stdout.split(/\r?\n/).map(line => line.match(/^\s*(?:;\s*)?([^=]+?)\s*=\s*(.*)$/)).filter(Boolean); \
      const credential = key => /(^|:)(_authToken|_auth|_password|password|username|cert|key|certfile|keyfile)$/i.test(key.trim()); \
      if (normalized.stdout.includes("(protected)") || entries.some(([, key, value]) => credential(key) || /https?:\/\/[^/\s]*@/i.test(value))) { throw new Error("Project .npmrc must contain only nonsecret options; registry credentials require a separate secret mechanism"); } \
      const needsSource = fs.existsSync("binding.gyp") || p.workspaces || hooks.some(h => (p.scripts || {})[h]) \
        || deps.some(d => Object.values(d || {}).some(local)) \
        || Object.values(lock.packages || {}).some(d => d.link || local(d.resolved)) \
        || entries.some(([, key]) => /^(script-shell|cafile)$/i.test(key.trim())); \
      fs.writeFileSync("/tmp/icicle-npm-mode", process.env.NPM_INSTALL_MODE === "full" || needsSource ? "full" : "manifests"); \
    '; \
    if [ "$NPM_INSTALL_MODE" = full ] || [ "$(cat /tmp/icicle-npm-mode)" = full ]; then \
      touch /tmp/icicle-npm-full-source; \
      echo "npm dependencies will be installed with full source"; \
    else \
      npm ci --include=dev; \
    fi

COPY . /app

# Required-file checks follow dependency installation so source-only changes do
# not invalidate the manifest install layer. COPY above reports missing manifests.
RUN --mount=type=cache,target=/root/.npm \
    set -eu; \
    test -f icicle-service.yaml || { echo "ERROR: icicle-service.yaml is missing" >&2; exit 1; }; \
    node -e 'process.exit((require("./package.json").scripts || {}).build ? 0 : 1)' \
      || { echo 'ERROR: package.json has no "build" script' >&2; exit 1; }; \
    if [ -f /tmp/icicle-npm-full-source ]; then \
      npm ci --include=dev; \
      rm /tmp/icicle-npm-full-source; \
    fi

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
FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS run-stage

# Shared-template provenance is independent of the application's build version.
ARG TEMPLATE_VERSION="development"
ARG TEMPLATE_REVISION="unknown"
LABEL io.icicle.template.version=${TEMPLATE_VERSION} \
    io.icicle.template.revision=${TEMPLATE_REVISION}

ARG RUNTIME_APT_PACKAGES=""
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      nginx \
      ca-certificates \
      gettext-base \
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

# CI fetches these shared sources into the service build-context root. The
# entrypoint generates writable configs under /tmp/nginx at container startup.
COPY nginx.conf nginx-api.conf.template /etc/nginx/templates/
COPY nginx-entrypoint.sh /usr/local/bin/icicle-frontend-entrypoint.sh

# The built static site.
COPY --from=build-stage --chown=icicle:icicle /app/dist /app/site

# Build metadata. Written to a file the /healthcheck endpoint can serve, since there
# is no application process to read an env var.
ARG BUILD_SHA="unknown"
RUN printf '%s' "${BUILD_SHA}" > /app/site/.build_sha \
    && chown icicle:icicle /app/site/.build_sha

USER icicle
WORKDIR /app

ENV PORT=5000
EXPOSE 5000

# SIGQUIT lets nginx finish active requests when the container is stopped.
STOPSIGNAL SIGQUIT
ENTRYPOINT ["sh", "/usr/local/bin/icicle-frontend-entrypoint.sh"]
