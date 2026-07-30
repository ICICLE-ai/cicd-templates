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
# │     --build-arg NODE_VERSION=22.11 \                                    │
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
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS build-stage

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

# Node version in MAJOR.MINOR form. Populated from icicle-service.yaml runtime-version.
# The newest patch release of that minor line is resolved from the official dist
# index and verified against the published SHASUMS256.txt before it is unpacked.
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
      echo "       Check https://nodejs.org/dist/index.json for valid MAJOR.MINOR lines." >&2; \
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

# Fail fast with a clear message if required files are missing.
RUN --mount=type=bind,source=.,target=/src,rw \
    test -f /src/icicle-service.yaml || (echo "ERROR: icicle-service.yaml is missing. See template header." && exit 1) \
    && test -f /src/package.json || (echo "ERROR: package.json is missing. See template header." && exit 1) \
    && test -f /src/package-lock.json || (echo "ERROR: package-lock.json is missing. Run 'npm install' in your repo." && exit 1) \
    && test -f /src/entrypoint.sh || (echo "ERROR: entrypoint.sh is missing. See template header." && exit 1)

# Install everything, including devDependencies, so the build script can run.
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=bind,source=package.json,target=package.json \
    --mount=type=bind,source=package-lock.json,target=package-lock.json \
    npm ci --include=dev

COPY . /app

# Run the service's build script if it defines one (TypeScript, bundlers, etc.).
# Services that ship plain JavaScript can omit it.
RUN set -eux; \
    if node -e 'process.exit(require("./package.json").scripts && require("./package.json").scripts.build ? 0 : 1)'; then \
      npm run build; \
    else \
      echo "No build script defined in package.json — skipping."; \
    fi

# Reinstall without devDependencies so only production deps ship in the image.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev


# ╭───────────╮
# │ Run-Stage │
# ╰───────────╯
FROM debian:trixie-20260421-slim@sha256:cedb1ef40439206b673ee8b33a46a03a0c9fa90bf3732f54704f99cb061d2c5a AS run-stage

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

ENV PATH="/node/bin:$PATH" \
    NODE_ENV=production

USER icicle
WORKDIR /app

ENTRYPOINT ["sh", "entrypoint.sh"]
