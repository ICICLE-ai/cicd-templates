# ╭─────────────────────────────────────────────────────────────────────────╮
# │ ICICLE Official Dockerfile for backend Node services                    │
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
# │   - package.json         (deps + optional "build" script)               │
# │   - package-lock.json    (run `npm install` to generate)                │
# │   - entrypoint.sh        (your service start script, must use `exec`)   │
# │   - .dockerignore        (copy from icicle-ai/cicd-templates)           │
# │                                                                         │
# │ LOCAL TEST:                                                             │
# │   docker build \                                                        │
# │     --build-arg NODE_VERSION=24.22.0 \                                    │
# │     --build-arg BUILD_APT_PACKAGES="" \                                 │
# │     --build-arg RUNTIME_APT_PACKAGES="" \                               │
# │     --build-arg BUILD_SHA=local-test \                                  │
# │     -t my-service:test .                                                │
# │   docker run --rm -p 8000:8000 my-service:test                          │
# │                                                                         │
# │ CI passes these build args automatically from icicle-service.yaml.      │
# │                                                                         │
# │ NEED SOMETHING THIS TEMPLATE DOESN'T SUPPORT?                           │
# │   Open an issue in github.com/icicle-ai/cicd-templates and ping         │
# │    #icicle-service-support on slack. Do not patch locally.              │
# │                                                                         │
# │ Ref: https://nodejs.org/en/download                                     │
# ╰─────────────────────────────────────────────────────────────────────────╯

# ╭─────────────╮
# │ Build-Stage │
# ╰─────────────╯
FROM debian:trixie-20261020-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS build-stage

# curl / xz-utils are needed to fetch and unpack the official Node tarball.
# Build-stage apt packages are appended from icicle-service.yaml build-apt-packages.
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
    test -f entrypoint.sh || { echo "ERROR: entrypoint.sh is missing" >&2; exit 1; }; \
    if [ -f /tmp/icicle-npm-full-source ]; then \
      npm ci --include=dev; \
      rm /tmp/icicle-npm-full-source; \
    fi

# Run the service's build script if it defines one (TypeScript, bundlers, etc.).
# Services that ship plain JavaScript can omit it.
RUN set -eux; \
    if node -e 'process.exit(require("./package.json").scripts && require("./package.json").scripts.build ? 0 : 1)'; then \
      npm run build; \
    else \
      echo "No build script defined in package.json — skipping."; \
    fi

# Preserve production lifecycle behavior (including NODE_ENV=production).
# Generic pruning can retain development-mode native/generated artifacts.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev \
    && find /app -type f -name .npmrc -delete


# ╭───────────╮
# │ Run-Stage │
# ╰───────────╯
FROM debian:trixie-20261020-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS run-stage

# Shared-template provenance is independent of the application's build version.
ARG TEMPLATE_VERSION="development"
ARG TEMPLATE_REVISION="unknown"
LABEL io.icicle.template.version=${TEMPLATE_VERSION} \
    io.icicle.template.revision=${TEMPLATE_REVISION}

# libatomic1 is required by the official Node binaries.
# Runtime apt packages are appended from icicle-service.yaml runtime-apt-packages.
ARG RUNTIME_APT_PACKAGES=""
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      libatomic1 \
      $RUNTIME_APT_PACKAGES; \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 icicle \
    && useradd --system --gid 999 --uid 999 --create-home icicle

COPY --from=build-stage /node /node
COPY --from=build-stage --chown=icicle:icicle /app /app

# Build metadata — populated by CI for the /health endpoint.
ARG BUILD_SHA="unknown"
ENV BUILD_SHA=${BUILD_SHA}
ARG BUILD_VERSION=""
ENV BUILD_VERSION=${BUILD_VERSION}

ENV PATH="/node/bin:$PATH" \
    NODE_ENV=production

USER icicle
WORKDIR /app

ENTRYPOINT ["sh", "entrypoint.sh"]
