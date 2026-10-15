# Quick start

Shared builds and deployment for Python services, Node servers, and static frontends. The release pin is **`v2.2.1`**.

New services use schema 3: `service-name` names both the image and deployment pod. Existing schemas 1 and 2 remain supported; see [configuration and migration](configuration.md).

Choose a runtime, then follow its short guide:

| Runtime | Use it for | Guide | Copy into your repo |
|---|---|---|---|
| `python` | Python APIs and services | [Python setup](../python/service-setup.md) | [`.dockerignore`](../python/.dockerignore) |
| `node-backend` | Node APIs or frontends that need a running Node server | [Node setup](../node-backend/service-setup.md) | [`.dockerignore`](../node-backend/.dockerignore) |
| `node-frontend` | Static sites built by Node and served by Nginx | [Frontend setup](../node-frontend/service-setup.md) | [`.dockerignore`](../node-frontend/.dockerignore) |

Before deploying, the platform team must provision a Tapis pod, allow its image repository, and configure networking and registry access. Your repository needs access to the organization secrets used below.

**1. Create `icicle-service.yaml` in your service repository.** This example is for Python; change the runtime fields for Node:

```yaml
schema-version: 3
service-name: myservice
service-version: 0.1.0
project-name: myproject
service-type: ai
runtime-type: python
runtime-version: "3.13.16"
include-dev-dependencies: false
build-apt-packages: []
runtime-apt-packages: []
```

For Node, select `node-backend` or `node-frontend` and a quoted Node version such as `"24.22.0"`. The frontend's Node version is used only during its build. Set `service-name` to your provisioned pod's name: 4–64 lowercase letters/digits, starting with a letter, excluding reserved Tapis names. Do not add `pod-name`.

**2. Add the files required by your runtime guide.** Copy its `.dockerignore`. Backends need an `entrypoint.sh`; the frontend receives its startup script from CI. CI supplies the authoritative Dockerfile and Nginx files, so service-local edits to downloaded copies will be replaced.

**3. Create `.github/workflows/deploy.yaml`:**

```yaml
name: Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  packages: write
jobs:
  deploy:
    uses: icicle-ai/cicd-templates/.github/workflows/deploy-service.yaml@v2.2.1
    secrets:
      TAPIS_TOKEN: ${{ secrets.TAPIS_TOKEN }}
      REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}
      REGISTRY_PASSWORD: ${{ secrets.REGISTRY_PASSWORD }}
      HEALTHCHECK_TOKEN: ${{ secrets.HEALTHCHECK_TOKEN }}
```

`TAPIS_TOKEN` is required. Omit both registry secrets to authenticate GHCR with `github.actor` and `github.token`; the package must grant this calling repository write access. To use another registry credential, provide both `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`. `HEALTHCHECK_TOKEN` is optional for health endpoints requiring a bearer token. You can also copy the [starter workflow](../deploy.yaml).

**4. Commit and push to your service's `main` branch.** Check the Actions run for build and rollout results.

For help, contact `#icicle-service-support` or open an [issue](https://github.com/icicle-ai/cicd-templates/issues).
