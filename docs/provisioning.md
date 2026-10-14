# ICICLE Deployments

Create pods and volumes from a reviewed catalog, then start, stop, or restart them through GitHub Actions. Developers receive Tapis **USER** access; both required institute accounts receive **ADMIN**. The stable automation account retains its access. All three runtimes are supported.

This feature is unreleased and is not included in `v2.1.0`.

## Platform setup

Use one trusted platform repository. Limit write access to trusted operators, protect its default branch, and create a GitHub environment named **`tapis`** with required platform reviewers and deployments restricted to the default branch. Workflows read the selected default-branch commit.

Add these organization secrets with visibility restricted to that platform repository:

| Secret | Required | Value |
|---|---|---|
| `TAPIS_PROVISION_TOKEN` | Yes | Stable automation account's token for the target tenant. It must create pods/volumes and have resource `ADMIN` access for lifecycle operations. |
| `TAPIS_REQUIRED_ADMINS` | Yes | JSON array of two distinct Tapis usernames, such as `["instituteadminone","instituteadmintwo"]`. |
| `SLACK_PROVISIONING_WEBHOOK_URL` | For Slack alerts | Incoming webhook URL for the platform team's channel. |
| `HEALTHCHECK_TOKEN` | If needed | Bearer token accepted by protected application health endpoints. |

Use canonical Tapis usernames. Required administrators keep `ADMIN` even if listed as developers. Image approval remains a separate platform action; the provisioning token does not need image-allowlist administrator privileges.

Set repository variables `TAPIS_BASE_URL` and `TAPIS_TENANT_ID` for another deployment. Defaults are `https://icicleai.tapis.io` and `icicleai`. Keep the tenant ID consistent with the API endpoint.

Other workflows in an authorized repository can use its organization secrets. Protecting these workflow files alone does not isolate the token. Do not expose it to service repositories. Existing application deployments keep their separate `TAPIS_TOKEN`; scope those credentials to their own pods rather than sharing the provisioning account.

## Add a service

Copy [the catalog example](../provisioning-example.yaml) to `provisioning/<pod-name>.yaml` and submit it for platform review. Catalog `schema-version: 1` is independent of application `icicle-service.yaml` schema 1/2.

Specify the runtime, already-built image, expected build SHA, developers, and optional volumes. Names need 4–64 lowercase letters/digits, starting with a letter. The catalog filename and new pod image repository name must match the pod name. Use unique image tags. Never commit tokens or passwords.

New pods set `PORT=5000`. Override `environment-variables.PORT` with a string from `"1024"` to `"65535"`; networking follows that value. Backends must read `PORT`. Frontends need the forthcoming template release to honor it; existing `v2.1.0` frontend images require `PORT: "8000"` until rebuilt.

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
