# Configuration

- New services use **schema 3** with templates **v2.2.0**. `service-name` names both the registry image and Tapis pod; omit `pod-name`. Use 4–64 lowercase ASCII letters/digits, starting with a letter. Spaces, hyphens, underscores, and reserved Tapis names such as `admin`, `images`, and `secrets` are rejected. Tapis manages internal container identifiers.
- Existing services can retain **schemas 1 and 2**. Schema 1 permits different service/pod names; schema 2 requires matching names. Onboarding review must require schema 3 because CI cannot infer whether a service is new.
- `service-version` is `MAJOR.MINOR.PATCH`. `project-name` identifies the project; `service-type` is `ai`, `ci`, or `domain`. Project and type are recorded as image labels.
- Quote `runtime-version` as `MAJOR.MINOR.PATCH` for an exact interpreter version (new examples: Python `"3.13.16"`, Node `"24.22.0"`). Existing `MAJOR.MINOR` values still resolve a patch on that minor line; legacy unquoted decimals are accepted with a warning. Package lists contain Debian package names, optionally with architecture or exact version.
- Framework, package name, source layout, and Node module format are application choices.

| Optional field | Default | Use |
|---|---|---|
| `health-url` | Discover from the pod's default or sole HTTP endpoint | Exact HTTP(S) health URL; no credentials, query, or fragment. |
| `rollout-timeout` | `300` | Verification timeout in seconds, from 30 to 1800. |
| `npm-install-mode` | `auto` | Node only. Detect source-dependent installs automatically; use `full` when unusual dependency hooks need application files. |
| `include-dev-dependencies` | `true` | Python only. Set `false` for new services; omission preserves legacy dependencies. |

See the [annotated configuration](../icicle-service-example.yaml) for field examples. Unknown extension fields warn and are ignored by CI; malformed values fail before building.

To migrate from schema 2, pin the workflow to **v2.2.0**, set `schema-version: 3`, and remove `pod-name`. Check the naming rules above; schema 3 also rejects reserved names. For schema 1 services with different names, coordinate matching image/pod names and image approval first. CI does not create or rename pods. See the [schema 3 example](../icicle-service-v3-example.yaml).

Build caches are isolated by calling repository, service, and runtime. Source-only edits reuse dependency layers on the fast path; manifests, lockfiles, and Node `.npmrc` changes invalidate them.

The first build is cold, and cache reuse depends on availability. GitHub stores the Docker layers; package-manager cache mounts are not exported, so a changed lockfile can require fresh downloads on a new runner. See [Docker's cache guidance](https://docs.docker.com/build/ci/github-actions/cache/#cache-mounts).

Node root lifecycle scripts, workspaces, local dependencies, and source-dependent npm configuration select a full-source install, where source changes intentionally reinstall dependencies. `npm-install-mode: full` forces that behavior. Non-secret project `.npmrc` options are supported; embedded registry credentials are rejected, and private npm registry secret injection is not provided. Node backends still run a second `npm ci --omit=dev` after building to preserve production lifecycle behavior.
