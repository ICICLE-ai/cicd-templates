# Changelog

## 2.0.0

- New services use schema 2: `service-name` (the registry image basename) and
  `pod-name` must match and use 4–64 lowercase letters/digits, starting with a
  letter. Schema 1 remains supported for existing services with separate names.
- All three Dockerfiles, the workflow, and frontend runtime files share one
  template release. Images record `io.icicle.template.version` and
  `io.icicle.template.revision`; the workflow fetches its own exact commit.
- Frontends can proxy complete `/api/...` paths to a runtime `BACKEND_URL`.
  Proxying stays optional; new frontend clients must use relative API paths.
- Deployments select a unique build tag, update only the existing pod's image,
  restart it, and verify a new instance serving the expected build SHA.
- Typed configuration validation rejects malformed names, package arguments,
  duplicate keys, and unsafe output values before the build.
- Python builds honor the configured interpreter and can omit dev dependencies
  with `include-dev-dependencies: false`. Omission preserves legacy behavior.
- CI includes configuration, rollout, HTTP/HTTPS proxy integration, and actual
  Docker image smoke tests for Python, Node backend, and Node frontend.

### Adopting this release

Pin the shared workflow to the release tag or its full commit and use the same
ref for manual template downloads. Template release version, configuration
schema version, and application `service-version` are independent.

Existing services can stay on schema 1. Before changing to schema 2, choose a
matching image/pod name and coordinate any registry allowlist or pod changes
with the platform owner. CI does not rename or create existing pods.

Adopting runtime frontend proxying requires one image build with these
templates. Subsequent `BACKEND_URL` changes only require a container restart.
