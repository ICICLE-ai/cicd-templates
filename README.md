# cicd-templates

> Canonical Dockerfiles, configs, and CI workflow for ICICLE services.

This repo is the **source of truth** for how ICICLE services get built and deployed. Every service repo's CI delegates to the workflow defined here, and fetches its Dockerfile from this repo at build time.

If you're a researcher onboarding a new service, jump to the per-runtime setup guide:

- **Python** → [`python/service-setup.md`](./python/service-setup.md)
- **Node** → coming soon
- **Bun** → coming soon

---

## What this repo provides

| Path | Purpose | Who uses it |
|---|---|---|
| `.github/workflows/deploy-service.yaml` | Reusable workflow that builds, pushes, and deploys any ICICLE service. Service repos call into it. | CI (every service repo) |
| `python/Dockerfile` | Official Dockerfile for Python services using uv. Fetched at build time. | CI |
| `python/.dockerignore` | Starter `.dockerignore` for Python services. Researchers copy this once. | Researchers |
| `python/entrypoint.sh` | Starter entrypoint script for Python services. Researchers copy and customize. | Researchers |
| `python/service-setup.md` | Step-by-step onboarding guide for Python service authors. | Researchers |
| `node/Dockerfile` | Official Dockerfile for Node services. *(coming soon)* | CI |
| `bun/Dockerfile` | Official Dockerfile for Bun services. *(coming soon)* | CI |
| `icicle-service.yaml` | Empty starter config. Researchers copy and fill in. | Researchers |
| `icicle-service-example.yaml` | Annotated example showing every field with comments. | Researchers (reference) |
| `deploy.yaml` | Example workflow for a service repo. Researchers copy this into their `.github/workflows/`. | Researchers |

---

## How a service uses this repo

The flow, from researcher push to deployed pod:

```
┌──────────────────────────────────────────────────────────┐
│ Researcher's service repo                                │
│   icicle-service.yaml      ← service config              │
│   pyproject.toml / etc.    ← deps                        │
│   src/                     ← code                        │
│   .github/workflows/       ← 5-line workflow that calls  │
│     deploy.yaml            │  the reusable workflow here │
└─────────────────────┬────────────────────────────────────┘
                      │  push to main
                      ▼
┌──────────────────────────────────────────────────────────┐
│ cicd-templates (THIS REPO)                               │
│   .github/workflows/deploy-service.yaml                  │
│     1. Parse + validate icicle-service.yaml              │
│     2. Fetch <runtime>/Dockerfile                        │
│     3. Build + push to ghcr.io                           │
│     4. PATCH Tapis pod with new image                    │
└─────────────────────┬────────────────────────────────────┘
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
   ┌─────────┐                 ┌─────────┐
   │  GHCR   │                 │  Tapis  │
   └─────────┘                 └─────────┘
```

Researchers don't write build pipelines. They commit a 5-line `deploy.yaml`, fill in `icicle-service.yaml`, and push. The reusable workflow does the rest.

---

## Versioning

This repo uses git tags to version its workflow and Dockerfiles together. Service repos pin to a tag:

```yaml
uses: icicle-ai/cicd-templates/.github/workflows/deploy-service.yaml@v1
```

The reusable workflow fetches Dockerfiles using the same tag, so the workflow and the Dockerfile it pulls always come from the same release.

**Current version:** `v1`

**Upgrading services to a new version:** researchers bump the `@v1` reference in their service's `deploy.yaml` to `@v2` (or whatever the new tag is). No other changes needed unless the new version introduced schema changes (called out in release notes).

---

## For maintainers

### Making changes

Anything in this repo affects every ICICLE service that pins to the affected tag. Process:

1. Open a PR against `main`. CI runs basic linting on the workflow.
2. Test against a real service by temporarily pointing its `deploy.yaml` at your branch (`@your-branch-name` instead of `@v1`).
3. Merge the PR.
4. **Tag a release** (`v2`, `v1.1`, etc.) when the change is ready for general use. Until tagged, no services see the change.
5. Announce in `#icicle-service-support` with what changed and migration notes if any.

### What can break a service

In rough order of impact:

- **Schema changes to `icicle-service.yaml`** — adding required fields breaks every existing service. Either make new fields optional with defaults, or bump the schema version and support both.
- **Removing or renaming Dockerfile build args** — services pinning to the new tag will fail at build time. Add new args, deprecate old ones over a release cycle.
- **Changing the validation regex** — services with previously-valid configs will start failing. Test against existing service repos before merging.
- **Bumping base image** — usually safe, but a major Debian/Python/Node version bump can break services that depend on specific system libraries. Test broadly.

### Adding a new runtime

To add support for a new runtime (Go, Rust, etc.):

1. Create `<runtime>/Dockerfile` following the patterns in `python/Dockerfile`.
2. Create `<runtime>/.dockerignore` and `<runtime>/entrypoint.sh` starters.
3. Add a case branch in the `Set up Buildtime Args` step in `.github/workflows/deploy-service.yaml`.
4. Add the runtime to the validation allow-list in the same workflow.
5. Write `<runtime>/service-setup.md` for researchers.
6. Tag a new version.

---

## Need something the templates don't support?

Open an issue in this repo and ping `#icicle-service-support` on Slack. **Don't fork the Dockerfile and commit your own.** The whole point of this repo is centralized control — local forks defeat that and put your service on a different upgrade track from everyone else.

---

## Contact

- Slack: `#icicle-service-support`
- Issues: [icicle-ai/cicd-templates/issues](https://github.com/icicle-ai/cicd-templates/issues)
