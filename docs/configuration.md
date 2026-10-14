# Configuration

- New services use **schema 2**. `service-name` and `pod-name` must be identical: 4–64 lowercase ASCII letters/digits, starting with a letter. No spaces, hyphens, or underscores. This names the registry image and Tapis pod; Tapis manages internal container identifiers.
- Existing services can retain **schema 1**, including different service/pod names. To migrate, coordinate the new image name and registry allowlist first; CI does not create or rename pods. Onboarding review must require schema 2 because CI cannot infer whether a service is new.
- `service-version` is `MAJOR.MINOR.PATCH`. `project-name` identifies the project; `service-type` is `ai`, `ci`, or `domain`. Project and type are recorded as image labels.
- Quote `runtime-version` as `MAJOR.MINOR`. Legacy unquoted decimals are accepted with a warning. Package lists contain Debian package names, optionally with architecture or exact version.
- Framework, package name, source layout, and Node module format are application choices.

| Optional field | Default | Use |
|---|---|---|
| `health-url` | Discover from the pod's default or sole HTTP endpoint | Exact HTTP(S) health URL; no credentials, query, or fragment. |
| `rollout-timeout` | `300` | Verification timeout in seconds, from 30 to 1800. |
| `include-dev-dependencies` | `true` | Python only. Set `false` for new services; omission preserves legacy dependencies. |

See the [annotated configuration](../icicle-service-example.yaml) for field examples. Unknown extension fields warn and are ignored by CI; malformed values fail before building.
