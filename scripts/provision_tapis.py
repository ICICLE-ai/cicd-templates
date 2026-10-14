"""Plan or create one managed Tapis pod and its volumes without replacing resources."""

import argparse
import os
import re
import sys
import time

from .deploy_tapis import (APIError, DeploymentError, HTTPClient, HTTPConnectionError,
                           api_result, checked_url, health_url, matches_health)
from .pod_config import load_config, required_admins
from .tapis_images import MissingImageApproval, ensure_image_allowed, notify_missing_image


LEVELS = {"READ": 1, "USER": 2, "ADMIN": 3, "APPROVEDADMIN": 4}
TRANSIENT = {408, 429, 500, 502, 503, 504}
POD_STATES = {"STOPPED", "AVAILABLE", "ERROR", "COMPLETE", "DELETING", "REQUESTED", "SPAWNING",
              "RESTARTING", "SHUTTING_DOWN"}


class Session:
    """Bound API reads and writes by one deadline; retry only safe reads."""

    def __init__(self, client, timeout, clock, sleep):
        self.client, self.clock, self.sleep = client, clock, sleep
        self.deadline = clock() + timeout

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise DeploymentError("Provisioning deadline expired; existing resources were retained")
        return min(10, remaining)

    def pause(self):
        self.sleep(min(2, max(0, self.deadline - self.clock())))

    def read(self, url, *, optional=False):
        for attempt in range(3):
            try:
                result = api_result(self.client, "GET", url, timeout=self.remaining())
                if not isinstance(result, dict):
                    raise DeploymentError("Tapis did not return the expected resource object")
                return result
            except (APIError, HTTPConnectionError) as error:
                if isinstance(error, APIError) and error.status == 404 and optional:
                    return None
                transient = isinstance(error, HTTPConnectionError) or error.status in TRANSIENT
                if not transient or attempt == 2:
                    raise
                self.sleep(min(2 ** attempt, max(0, self.deadline - self.clock())))

    def write(self, method, url, payload=None):
        # GET /start is a mutation too. Never retry it or a permission update.
        return api_result(self.client, method, url, payload, timeout=self.remaining())


def permissions(resource):
    values = resource.get("permissions")
    if not isinstance(values, list):
        raise DeploymentError("Tapis did not return a permissions list")
    result = {}
    for entry in values:
        if not isinstance(entry, str) or ":" not in entry:
            raise DeploymentError("Tapis returned an invalid permission entry")
        user, level = entry.rsplit(":", 1)
        if not user or level not in LEVELS or user in result:
            raise DeploymentError("Tapis returned ambiguous or unsupported permissions")
        result[user] = level
    return result


def grants_needed(current, desired):
    """Never demote an existing administrator or claim they have only USER access."""
    changes = {}
    for user, level in desired.items():
        actual = current.get(user)
        if level == "USER" and LEVELS.get(actual, 0) > LEVELS[level]:
            raise DeploymentError("A configured developer already has administrator access; "
                                  "an administrator must resolve that permission before provisioning")
        if LEVELS.get(actual, 0) < LEVELS[level]:
            changes[user] = level
    return changes


def grant_permissions(session, url, desired):
    before = permissions(session.read(url + "/permissions"))
    for user, level in grants_needed(before, desired).items():
        session.write("POST", url + "/permissions", {"user": user, "level": level})
    after = permissions(session.read(url + "/permissions"))
    if grants_needed(after, desired):
        raise DeploymentError("Required permissions were not persisted; pod start was not requested")
    if any(LEVELS.get(after.get(user), 0) < LEVELS[level] for user, level in before.items()):
        raise DeploymentError("Existing permissions changed unexpectedly; pod start was not requested")
    return after


def payloads(config, owner):
    name = config["pod-name"]
    marker = f"Managed by cicd-templates: {owner}/{name}"
    volumes = [{"volume_id": volume["volume-id"], "description": marker,
                "size_limit": volume["size-limit"]} for volume in config["volumes"]]
    mounts = {volume["mount-path"]: {"type": "tapisvolume", "source_id": volume["volume-id"],
                                     "read_only": volume["read-only"]}
              for volume in config["volumes"]}
    pod = {"pod_id": name, "description": marker, "image": config["image"],
           "status_requested": "OFF", "networking": {"default": {"protocol": "http", "port": config["port"]}},
           "environment_variables": config["environment-variables"], "resources": config["resources"],
           "time_to_stop_default": config["time-to-stop-default"], "volume_mounts": mounts}
    return pod, volumes


def check_volume(actual, expected):
    if any(actual.get(key) != value for key, value in expected.items()):
        raise DeploymentError("Existing volume does not match this owner and configuration; "
                              "automatic adoption or modification is not supported")


def check_pod(actual, expected):
    for key in ("pod_id", "description", "image", "environment_variables", "time_to_stop_default"):
        if actual.get(key) != expected[key]:
            raise DeploymentError(f"Existing pod does not match the planned {key}; "
                                  "use the deployment workflow for image updates, not provisioning")
    # Generated networking URLs, mount identities, and default resource fields are expected.
    for key in ("resources", "networking", "volume_mounts"):
        value = actual.get(key)
        if not isinstance(value, dict):
            raise DeploymentError(f"Existing pod has invalid {key}")
        if key != "resources" and value.keys() != expected[key].keys():
            raise DeploymentError(f"Existing pod has different {key}; automatic modification is not supported")
        for name, wanted in expected[key].items():
            observed = value.get(name)
            if isinstance(wanted, dict):
                if not isinstance(observed, dict) or any(observed.get(k) != v for k, v in wanted.items()):
                    raise DeploymentError(f"Existing pod has different {key}; automatic modification is not supported")
                if key == "volume_mounts" and (observed.get("sub_path") or observed.get("config_content")):
                    raise DeploymentError("Existing pod mount configuration was modified")
            elif observed != wanted:
                raise DeploymentError(f"Existing pod has different {key}; automatic modification is not supported")
    if any(actual.get(key) for key in ("template", "command", "arguments", "template_overrides")):
        raise DeploymentError("Existing pod has unsupported command or template overrides")


def create_or_recover(session, collection, url, expected, check):
    try:
        session.write("POST", collection, expected)
    except DeploymentError as error:
        # A lost create response is ambiguous: inspect the exact ID, never repeat POST.
        if isinstance(error, APIError) and error.status not in TRANSIENT | {409}:
            raise
        actual = session.read(url, optional=True)
        if actual is None:
            raise error
        check(actual, expected)
        return actual
    actual = session.read(url)
    check(actual, expected)
    return actual


def volume_available(session, url, expected):
    while True:
        actual = session.read(url)
        check_volume(actual, expected)
        if actual.get("status") == "AVAILABLE":
            return actual
        if actual.get("status") in {"ERROR", "DELETING", "DELETED"}:
            raise DeploymentError("Volume is unavailable; existing storage was retained")
        session.pause()


def verify_mount_permissions(pod, pod_acl, volume_acls):
    for mount in pod.get("volume_mounts", {}).values():
        identity = mount.get("mounted_by")
        if not isinstance(identity, str) or not identity:
            raise DeploymentError("Tapis did not return a mount identity; verify the deployed API before starting")
        if (LEVELS.get(pod_acl.get(identity), 0) < LEVELS["USER"]
                or LEVELS.get(volume_acls[mount["source_id"]].get(identity), 0) < LEVELS["READ"]):
            raise DeploymentError("A volume mount identity lacks required pod or volume access; "
                                  "pod start was not requested")


def plan_lines(config, plan):
    """Describe reviewable changes without environment values or secret administrator names."""
    resources = config["resources"]
    lifetime = config["time-to-stop-default"]
    lifetime_text = "unlimited" if lifetime == -1 else f"{lifetime} seconds"
    lines = [
        f"{'Apply' if plan['apply'] else 'Plan'}: {'create' if plan['create_pod'] else 'resume'} pod {plan['pod']}",
        f"Image: {config['image']}; runtime: {config['runtime-type']}; PORT: {config['port']}; lifetime: {lifetime_text}",
        f"Resources: CPU request {resources['cpu_request']}m / limit {resources['cpu_limit']}m; "
        f"memory request {resources['mem_request']} MiB / limit {resources['mem_limit']} MiB; "
        f"GPUs: {resources.get('gpus', 0)}",
    ]
    for volume in config["volumes"]:
        state = "create" if volume["volume-id"] in plan["create_volumes"] else "existing"
        lines.append(f"Volume {volume['volume-id']}: {state}; mount {volume['mount-path']}; "
                     f"size-limit {volume['size-limit']} MB (informational); "
                     f"read-only: {str(volume['read-only']).lower()}")
    if not config["volumes"]:
        lines.append("Volumes: none")
    admin_count = sum(level == "ADMIN" for level in plan["permissions"].values())
    developer_count = sum(level == "USER" for level in plan["permissions"].values())
    lines.append(f"Permissions: {admin_count} required ADMIN accounts; {developer_count} developer USER accounts; "
                 "retain existing permissions")
    return lines


def wait_healthy(session, pod_url, expected, config, before, started, log):
    previous = before.get("start_instance_ts")
    observed_transition = started and before.get("status") == "STOPPED"
    last_state = None
    while True:
        pod = session.read(pod_url)
        check_pod(pod, expected)
        state = pod.get("status")
        if not isinstance(state, str) or state not in POD_STATES:
            state = "UNKNOWN"
        if state != last_state:
            log(f"Pod status: {state}")
            last_state = state
        if state in {"ERROR", "COMPLETE", "DELETING"}:
            raise DeploymentError("Pod failed to become available; inspect its logs. Volumes were retained")
        if state in {"REQUESTED", "SPAWNING"}:
            observed_transition = True
        timestamp = pod.get("start_instance_ts")
        new_instance = not started or (timestamp != previous if timestamp is not None else observed_transition)
        container = pod.get("status_container") or {}
        if not isinstance(container, dict):
            raise DeploymentError("Tapis returned an invalid readiness response")
        ready = container.get("ready") is not False
        if (state == "AVAILABLE" and pod.get("status_requested", "ON") == "ON"
                and new_instance and ready and pod.get("networking_live") is not False):
            endpoint = health_url(pod, config["runtime-type"], config["health-url"])
            try:
                status, body = session.client.request("GET", endpoint, timeout=session.remaining())
            except HTTPConnectionError:
                pass
            else:
                if status == 200 and matches_health(body, config["runtime-type"], config["build-sha"]):
                    final = session.read(pod_url)
                    check_pod(final, expected)
                    final_container = final.get("status_container") or {}
                    if (final.get("status") == "AVAILABLE" and final.get("status_requested", "ON") == "ON"
                            and final.get("networking_live") is not False
                            and isinstance(final_container, dict) and final_container.get("ready") is not False
                            and (timestamp is None or final.get("start_instance_ts") == timestamp)):
                        log(f"Verified {config['pod-name']} at build {config['build-sha']}")
                        return endpoint
        session.pause()


def provision(client, config, admins, *, base_url, tenant, owner, apply=False,
              clock=time.monotonic, sleep=time.sleep, log=print):
    """Create missing resources or resume a matching creation; never update or delete them."""
    base = checked_url(base_url).rstrip("/")
    if not base.startswith("https://"):
        raise DeploymentError("Tapis API must use HTTPS")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", owner):
        raise DeploymentError("A trusted organization/repository owner is required")
    if config["image"].rsplit("/", 1)[-1].split(":", 1)[0] != config["pod-name"]:
        raise DeploymentError("New pod image repository name must match pod-name")
    if len(admins) != 2 or len(set(admins)) != 2:
        raise DeploymentError("Exactly two distinct institute administrators are required")
    session = Session(client, config["rollout-timeout"], clock, sleep)
    ensure_image_allowed(client, base_url=base, tenant=tenant, image=config["image"],
                         timeout=session.remaining())
    expected, volumes = payloads(config, owner)
    pod_url = base + "/v3/pods/" + config["pod-name"]
    volume_base = base + "/v3/pods/volumes"
    desired = {user: "USER" for user in config["developers"]}
    desired.update({user: "ADMIN" for user in admins})
    # Preflight every object before writing anything, including ACL conflicts.
    pod = session.read(pod_url, optional=True)
    if pod is not None:
        check_pod(pod, expected)
        grants_needed(permissions(session.read(pod_url + "/permissions")), desired)
    existing = {}
    for volume in volumes:
        url = volume_base + "/" + volume["volume_id"]
        existing[volume["volume_id"]] = session.read(url, optional=True)
        if pod is not None and existing[volume["volume_id"]] is None:
            raise DeploymentError("A volume referenced by an existing pod is missing; "
                                  "automatic storage recreation is not supported")
        if existing[volume["volume_id"]] is not None:
            check_volume(existing[volume["volume_id"]], volume)
            grants_needed(permissions(session.read(url + "/permissions")), desired)
    plan = {"pod": config["pod-name"], "create_pod": pod is None,
            "create_volumes": [v["volume_id"] for v in volumes if existing[v["volume_id"]] is None],
            "permissions": desired, "apply": apply}
    for line in plan_lines(config, plan):
        log(line)
    if not apply:
        return plan
    volume_acls = {}
    for volume in volumes:
        url = volume_base + "/" + volume["volume_id"]
        if existing[volume["volume_id"]] is None:
            create_or_recover(session, volume_base, url, volume, check_volume)
        volume_available(session, url, volume)
        volume_acls[volume["volume_id"]] = grant_permissions(session, url, desired)
    if pod is None:
        pod = create_or_recover(session, base + "/v3/pods", pod_url, expected, check_pod)
        if pod.get("status_requested") != "OFF" or pod.get("status") != "STOPPED":
            raise DeploymentError("New pod was not created stopped; start was not requested")
    pod_acl = grant_permissions(session, pod_url, desired)
    before = session.read(pod_url)
    check_pod(before, expected)
    verify_mount_permissions(before, pod_acl, volume_acls)
    started = False
    if before.get("status_requested") == "OFF" and before.get("status") == "STOPPED":
        session.write("GET", pod_url + "/start")
        started = True
    elif before.get("status_requested") != "ON" or before.get("status") not in {"AVAILABLE", "SPAWNING", "REQUESTED"}:
        raise DeploymentError("Pod has a conflicting lifecycle state; start was not requested")
    endpoint = wait_healthy(session, pod_url, expected, config, before, started, log)
    return {**plan, "health_url": endpoint, "build_sha": config["build-sha"], "image": config["image"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-url", default=os.environ.get("TAPIS_BASE_URL", "https://icicleai.tapis.io"))
    parser.add_argument("--tenant", default=os.environ.get("TAPIS_TENANT_ID", ""))
    parser.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--apply", action="store_true", help="Create resources; otherwise only plan")
    args = parser.parse_args()
    token = os.environ.get("TAPIS_TOKEN", "")
    if not token:
        parser.error("TAPIS_TOKEN is required in the environment")
    try:
        config = load_config(args.config)
        admins = required_admins(os.environ.get("TAPIS_REQUIRED_ADMINS", ""))
        result = provision(HTTPClient(token, os.environ.get("HEALTHCHECK_TOKEN", "")), config, admins,
                           base_url=args.base_url, tenant=args.tenant, owner=args.owner, apply=args.apply)
    except MissingImageApproval as error:
        if args.apply:
            notify_missing_image(error, os.environ)
        print(f"Provisioning blocked: {error}", file=sys.stderr)
        return 1
    except (DeploymentError, ValueError) as error:
        print(f"Provisioning failed: {error}", file=sys.stderr)
        return 1
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            if args.apply:
                output.write(f"Verified `{result['pod']}` at build `{result['build_sha']}`.\n")
            else:
                for line in plan_lines(config, result):
                    output.write(line + "\n\n")
                output.write("No resources changed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
