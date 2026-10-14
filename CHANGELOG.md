# Release notes

## 2.0.0

- Schema 2 requires matching service/image and pod names: 4–64 lowercase letters/digits, starting with a letter. Schema 1 remains supported.
- All three Dockerfiles, the workflow, and startup files share one version. Images record the template version and exact commit.
- Frontends can proxy full `/api/...` paths to runtime `BACKEND_URL`, with HTTP and verified HTTPS support. Proxying remains optional.
- CI deploys a unique build tag and verifies a new pod instance serving the expected build SHA.
- Configuration validation checks types, names, package arguments, and duplicate keys before building.
- Python honors the configured interpreter. `include-dev-dependencies: false` excludes the dev group; omission preserves existing behavior.
- CI tests all three real runtime images, alongside configuration, rollout, and HTTP/HTTPS proxy checks.

### Adoption

Pin the reusable workflow to `@v2.0.0` and use the same ref for manual downloads. Follow the current [quick start](README.md#quick-start); the original tagged starter calls `@main`, so replace that reference when copying it directly.

Existing services may stay on schema 1. Before migrating to schema 2, coordinate image names and registry access with the platform owner. CI does not create or rename pods.

Frontend proxy support needs one initial image build. Later `BACKEND_URL` changes need only an environment update and restart of the same image.
