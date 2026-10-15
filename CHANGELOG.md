# Release notes

## 2.2.0

- Add an optional reviewed platform catalog for provisioning pods and volumes across all three runtimes. Keep developers at `USER`, require two institute administrators, and verify permissions before starting a new pod.
- Add protected manual start/stop/restart workflows and Slack notifications when an image needs approval. Existing application deployment and schema 1/2 configurations remain unchanged.
- Default new provisioned pods to `PORT=5000`, with a runtime override shared by networking and the application. Frontend Nginx honors `PORT` while preserving `8000` when it is unset.

Pin workflows and manual downloads to `v2.2.0`. Existing schema 1/2 configurations remain supported. Frontend runtime `PORT` requires rebuilding with this release; older images continue to use port `8000`. See the [quick start](docs/quick-start.md) and [provisioning guide](docs/provisioning.md).

## 2.1.0

- Reuse dependency layers and repository/service/runtime build caches; preserve source-dependent npm installs and production lifecycle scripts. Node supports `npm-install-mode: auto|full` and non-secret `.npmrc`.
- Accept exact runtime patch pins alongside existing minor pins; new examples use Python 3.13.16 and Node 24.22.0, with digest-pinned uv 0.12.23 and base images.
- Add bounded frontend upload/timeouts, optional streaming and WebSockets, and immutable caching only for content-versioned asset filenames. Existing proxy defaults remain compatible.
- Default GHCR authentication to the calling repository's GitHub token; optional explicit registry credentials must be supplied together.
- Retry safe deployment reads and skip stale automatic push rollouts; manual dispatch supports an older ref for rollback.
- Always report **Template checks**, skip heavy builds for documentation-only edits, and maintain exact Actions/Docker pins with Dependabot.

Pin workflows and manual downloads to `v2.1.0`. Existing schema 1/2 configs and `MAJOR.MINOR` runtime values remain supported; upgrade deliberately using the [quick start](docs/quick-start.md).

## 2.0.0

- Schema 2 requires matching service/image and pod names: 4–64 lowercase letters/digits, starting with a letter. Schema 1 remains supported.
- All three Dockerfiles, the workflow, and startup files share one version. Images record the template version and exact commit.
- Frontends can proxy full `/api/...` paths to runtime `BACKEND_URL`, with HTTP and verified HTTPS support. Proxying remains optional.
- CI deploys a unique build tag and verifies a new pod instance serving the expected build SHA.
- Configuration validation checks types, names, package arguments, and duplicate keys before building.
- Python honors the configured interpreter. `include-dev-dependencies: false` excludes the dev group; omission preserves existing behavior.
- CI tests all three real runtime images, alongside configuration, rollout, and HTTP/HTTPS proxy checks.

### Adoption

Pin the reusable workflow to `@v2.0.0` and use the same ref for manual downloads. Follow the current [quick start](docs/quick-start.md#quick-start); the original tagged starter calls `@main`, so replace that reference when copying it directly.

Existing services may stay on schema 1. Before migrating to schema 2, coordinate image names and registry access with the platform owner. CI does not create or rename pods.

Frontend proxy support needs one initial image build. Later `BACKEND_URL` changes need only an environment update and restart of the same image.
