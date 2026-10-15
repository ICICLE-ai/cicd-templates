# Versioning

Pin the reusable workflow and manual template downloads to **`v2.2.0`**. CI fetches the Dockerfiles, startup files, and helpers from the workflow's exact commit. Update the pin deliberately when adopting another tested release.

These settings are independent:

| Version | Meaning |
|---|---|
| Template release (`v2.2.0`) | Shared Dockerfiles, workflow, and runtime files. |
| `schema-version` | Rules for the service configuration; this release supports 1 and 2. |
| `service-version` | Application release. |
| `runtime-version` | Python or Node interpreter/toolchain version. |

Images record `io.icicle.template.version` and `io.icicle.template.revision`. CI supplies the corresponding `TEMPLATE_VERSION` and `TEMPLATE_REVISION` build arguments; manual builds default to `development` and `unknown`. The published `v1` predates this setup. See [release notes](../CHANGELOG.md).

## Maintaining the templates

Make shared build and startup changes here. Before publishing a release, run the checks below and verify deployment against a provisioned test pod:

```bash
python3 -m venv /tmp/icicle-template-tests
. /tmp/icicle-template-tests/bin/activate
python -m pip install -r scripts/requirements.txt
python -m unittest discover -s tests -v
REQUIRE_DOCKER=1 python tests/test_images.py -v
```

Use Python 3.9+, Nginx with HTTP SSL support, `envsubst`, and OpenSSL. `NGINX_BINARY` can select a local Nginx binary. Image tests require Docker with BuildKit and network access. The always-running **Template checks** job requires Wiki generation and all selected tests. Documentation-only changes skip runtime and image builds. CI runs both suites for code changes; mocked Tapis tests do not replace a live rollout check.

Official GitHub Actions are pinned to exact commit SHAs; Docker bases and uv are digest-pinned. Dependabot proposes weekly Actions and Docker updates; review and test them before merging.

Update `VERSION`, the changelog, and onboarding pins together in the release commit. After checks pass, publish its exact `vMAJOR.MINOR.PATCH` tag. Release policy requires **Template checks** and one PR approval on `main`, with exact `vMAJOR.MINOR.PATCH` tags protected against updates and deletion. Never move a published exact tag. Existing services upgrade explicitly.

The [Publish Wiki workflow](../.github/workflows/publish-wiki.yaml) updates the Wiki automatically when Markdown, the renderer, or the publishing workflow changes on `main`. Pull requests check generation without publishing. Edit documentation here; no extra token or manual refresh is needed. To retry, run **Publish Wiki** from Actions on `main`. For a local preview, run `python3 scripts/render_wiki.py /tmp/wiki-preview`.

For help, contact `#icicle-service-support` or open an [issue](https://github.com/icicle-ai/cicd-templates/issues).
