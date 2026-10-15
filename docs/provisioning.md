# ICICLE Deployments

Create pods and volumes from a reviewed catalog, then start, stop, or restart them through GitHub Actions. Developers receive Tapis **USER** access; both required institute accounts receive **ADMIN**. The stable automation account retains its access. All three runtimes are supported.

Available in template release **`v2.2.0`**. Existing service deployment workflows remain compatible.

## Platform setup

Use one trusted platform repository. Limit write access to trusted operators, protect its default branch, and create a GitHub environment named **`tapis`** with required platform reviewers and deployments restricted to the default branch. Workflows read the selected default-branch commit.

Keep shared credentials and policy in **organization secrets**. Use **Selected repositories** to grant each secret where it is used:

| Organization secret | Repository access | Value |
|---|---|---|
| `TAPIS_TOKEN` | Platform repository and service repositories that deploy | Existing automation account's token. Provisioning needs pod/volume creation and resource `ADMIN` access for lifecycle operations. |
| `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` | Service repositories that need this credential pair | Optional shared registry credentials; GHCR builds can use the calling repository's `GITHUB_TOKEN` instead. |
| `TAPIS_REQUIRED_ADMINS` | Platform repository only | Required JSON array of two distinct Tapis usernames, such as `["instituteadminone","instituteadmintwo"]`. |
| `SLACK_PROVISIONING_WEBHOOK_URL` | Platform repository only | Incoming webhook URL for missing image approval alerts. |

Reuse `TAPIS_TOKEN`; no additional provisioning token is required. Keep service repositories' access to this token so their existing deployments continue working.

Keep service-specific values in **repository secrets**:

| Repository secret | Repository | Purpose |
|---|---|---|
| `POD_ENV_<UPPERCASE_POD_NAME>` | Platform repository | Optional JSON object of runtime secrets for one pod. For `myservice`, use `POD_ENV_MYSERVICE`. |
| `HEALTHCHECK_TOKEN` | Service repository | Optional bearer token for deployment health checks. Add the matching token to the platform repository or its `tapis` environment when central operations check that endpoint. |

Central health checks currently use one `HEALTHCHECK_TOKEN` for all services; per-pod runtime secrets are selected separately.

Actions reads all these scopes through `${{ secrets.NAME }}`; there is no separate organization-secret namespace. Environment secrets override repository secrets, which override organization secrets. Avoid duplicate names unless an override is intentional. See [GitHub's secret precedence rules](https://docs.github.com/en/actions/reference/security/secrets).

Use canonical Tapis usernames. Required administrators keep `ADMIN` even if listed as developers. Image approval remains a separate platform action; provisioning does not require image-allowlist administrator privileges.

Set repository variables `TAPIS_BASE_URL` and `TAPIS_TENANT_ID` for another deployment. Defaults are `https://icicleai.tapis.io` and `icicleai`. Keep the tenant ID consistent with the API endpoint.

Other workflows in an authorized repository can use its organization secrets. Repository access settings control availability; these workflows cannot enforce a secret's organization or repository scope.

## Add a service

Copy [the catalog example](../provisioning-example.yaml) to `provisioning/<pod-name>.yaml` and submit it for platform review. Catalog `schema-version: 1` is independent of application `icicle-service.yaml` schema 1/2.

Specify the runtime, already-built image, expected build SHA, developers, and optional volumes. Developer usernames, `PORT`, and `BACKEND_URL` are ordinary configuration, not Actions secrets; keep credentials out of URLs. Names need 4–64 lowercase letters/digits, starting with a letter. The catalog filename and new pod image repository name must match the pod name. Use unique image tags. Never commit tokens or passwords.

New pods set `PORT=5000`. Override `environment-variables.PORT` with a string from `"1024"` to `"65535"`; networking follows that value. Backends must read `PORT`. Frontends built with `v2.2.0` honor it; existing `v2.1.0` frontend images require `PORT: "8000"` until rebuilt with `v2.2.0` or later.

Declare required runtime secret names in the catalog:

```yaml
secret-environment-keys: [DATABASE_URL, API_KEY]
```

Then open the **platform repository → Settings → Secrets and variables → Actions → New repository secret**. Name it `POD_ENV_MYSERVICE` for pod `myservice` and enter a JSON object such as:

```json
{
  "DATABASE_URL": "replace-with-the-connection-string",
  "API_KEY": "replace-with-the-real-value"
}
```

Use string values and environment variable names containing letters, digits, or underscores, starting with a letter or underscore. The JSON must be at most 32 KiB, with no duplicate keys. Secret keys cannot overlap catalog environment variables; `PORT` always belongs in the catalog. Keep nonsecret `BACKEND_URL` there too. GitHub secret names are case-insensitive; uppercase names make the convention clear.

Provisioning selects the secret using the validated pod name and requires every declared key to have a nonempty string value. If no keys are declared, absent or empty JSON remains optional. Values enter the new pod's ordinary Tapis `environment_variables`, not a write-only secret store; accounts allowed to read pod configuration may see them. The workflow never prints values or uses them as frontend build inputs.

Reruns check existing environment values without changing them. Updating the GitHub secret alone does not rotate a running pod's credentials; update the pod environment deliberately through the platform and restart it. Lifecycle operations preserve the existing environment.

After merging, open **Actions → Provision pods and volumes** on the default branch:

1. Enter the pod name. Leave **apply** unchecked for a read-only preview; previews send no Slack messages.
2. Review the plan and rerun with **apply** checked.
3. An unapproved image stops creation and triggers Slack when configured. An administrator approves its repository in Tapis, then reruns the workflow. Slack reports the image, tenant, and run link; it does not approve images.
4. The workflow creates volumes and a stopped pod, verifies required permissions, then starts the pod and checks health and the build SHA.

Reruns resume owned resources. Unrelated resources are not adopted; volumes and existing users are never automatically deleted. Removing a developer requires separate access revocation.

## Start, stop, or restart

Open **Actions → Start, stop, or restart a pod** on the default branch. Choose the catalog pod and operation. Start/restart verify health and the current image's commit; supply **build-sha** for images without a `sha-<commit>-<run>-<attempt>` tag. Stop verifies shutdown.

The automation account performs lifecycle operations. Do not rely on Tapis `USER` for direct lifecycle calls. GitHub dispatch and environment approval access are assigned separately.

Platform operations serialize per tenant within this repository. Avoid simultaneous application deployments to the same pod: locks do not cross repositories. Normal service deployment remains image-only.

## Before the first production service

Pilot the target Tapis version, permissions, networking, and registry pull access. The catalog explicitly sets `time-to-stop-default: -1` instead of Tapis's default 12-hour lifetime; change it if required.

Test volume writes as **UID/GID 999** and persistence after restart. Prefer `/data`; mounting over `/app` hides application files. Volume `size-limit` is not an enforced quota. Keep the mount creator authorized on pods and volumes or restarts can fail. Coordinate shared volumes, backups, and ownership through the platform team.
