"""Operate an existing Tapis pod through its supported lifecycle endpoints."""

import argparse
import os
import re
import sys
import time

from .deploy_tapis import (
    DeploymentError, HTTPClient, api_result, checked_url, health_url,
    matches_health, read_pod,
)
from .pod_config import load_config
from .tapis_images import MissingImageApproval, ensure_image_allowed, notify_missing_image


RUNTIMES = {"python", "node-backend", "node-frontend"}
TRANSITION_STATES = {"RESTARTING", "SHUTTING_DOWN", "DELETING", "REQUESTED", "SPAWNING"}
KNOWN_STATES = TRANSITION_STATES | {"STOPPED", "AVAILABLE", "ERROR", "COMPLETE"}


def pod_state(pod):
    value = pod.get("status")
    return value if isinstance(value, str) and value in KNOWN_STATES else "UNKNOWN"


def expected_build(image, supplied=""):
    """Use the live image tag, never a potentially stale provisioning SHA."""
    if supplied and not re.fullmatch(r"[0-9a-f]{40}", supplied):
        raise DeploymentError("Expected a full lowercase Git build SHA")
    match = re.search(r":sha-([0-9a-f]{40})-[0-9]+-[0-9]+$", image)
    tagged = match.group(1) if match else ""
    if supplied and tagged and supplied != tagged:
        raise DeploymentError("--build-sha does not match the currently configured image tag")
    if not tagged and not supplied:
        raise DeploymentError("Current image has no build SHA tag; provide --build-sha for start or restart")
    return tagged or supplied


def operate(client, config, action, *, base_url, tenant, build_sha="", timeout=None,
            interval=5, clock=time.monotonic, sleep=time.sleep, log=print):
    """Issue at most one lifecycle mutation and verify the resulting pod state."""
    if action not in {"start", "stop", "restart"}:
        raise DeploymentError("Action must be start, stop, or restart")
    pod_name = config.get("pod-name", "")
    if not re.fullmatch(r"[a-z][a-z0-9]{3,63}", pod_name):
        raise DeploymentError("Invalid pod-name")
    runtime = config.get("runtime-type", "")
    if runtime not in RUNTIMES:
        raise DeploymentError("Unsupported runtime")
    timeout = config.get("rollout-timeout", 300) if timeout is None else timeout
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise DeploymentError("Lifecycle timeout must be positive")
    if interval <= 0:
        raise DeploymentError("Lifecycle polling interval must be positive")
    base = checked_url(base_url).rstrip("/")
    if not base.startswith("https://"):
        raise DeploymentError("Tapis API must use HTTPS")
    pod_url = base + "/v3/pods/" + pod_name
    deadline = clock() + timeout

    def read():
        return read_pod(client, pod_url, deadline=deadline, clock=clock, sleep=sleep)

    before = read()
    image = before.get("image")
    if action != "stop" and (not isinstance(image, str) or not image):
        raise DeploymentError("Pod has no configured image")
    endpoint = ""
    expected_sha = ""
    if action != "stop":
        expected_sha = expected_build(image, build_sha)
        endpoint = health_url(before, runtime, config.get("health-url", ""))
        if clock() >= deadline:
            raise DeploymentError("Lifecycle deadline expired before checking image approval")
        ensure_image_allowed(client, base_url=base_url, tenant=tenant, image=image,
                             timeout=min(10, deadline - clock()))

    # A preflight can take time. Recheck the target immediately before the
    # mutation, and throughout verification, so a concurrent deploy is visible.
    current = read()
    if current.get("image") != image:
        raise DeploymentError("Pod image changed during lifecycle preflight; no action was requested")
    previous_state = pod_state(current)
    previous_start = current.get("start_instance_ts")
    requested_state = current.get("status_requested")
    if action != "stop":
        endpoint = health_url(current, runtime, config.get("health-url", ""))
        pending = (previous_state in TRANSITION_STATES
                   or requested_state not in (None, "ON", "OFF")
                   or previous_state == "STOPPED" and requested_state == "ON"
                   or previous_state == "AVAILABLE" and requested_state == "OFF")
        if pending:
            raise DeploymentError("Pod already has a lifecycle operation in progress; wait before retrying")
    noop = action == "stop" and previous_state == "STOPPED" and requested_state in (None, "OFF")
    noop = noop or action == "start" and previous_state == "AVAILABLE"
    if noop and action == "stop":
        log(f"Pod {pod_name} is already stopped")
        return {"action": action, "pod": pod_name, "image": image, "unchanged": True}
    if not noop:
        if clock() >= deadline:
            raise DeploymentError("Lifecycle deadline expired before requesting action")
        # These GET routes mutate state. Never send them through read retries:
        # a lost acknowledgement must not cause another start/stop/restart.
        api_result(client, "GET", pod_url + "/" + action,
                   timeout=min(10, deadline - clock()))
        log(f"Requested {action} for {pod_name}")

    observed_transition = False
    last_state = None
    last_problem = "waiting for pod state"
    while clock() < deadline:
        pod = read()
        if pod.get("image") != image:
            raise DeploymentError("Pod image changed during lifecycle verification; another deployment may be running")
        state = pod_state(pod)
        if state != last_state:
            log(f"Pod status: {state}")
            last_state = state
        if action == "stop":
            if state == "STOPPED" and pod.get("status_requested") in (None, "OFF"):
                log(f"Verified {pod_name} is stopped")
                return {"action": action, "pod": pod_name, "image": image, "unchanged": False}
            last_problem = "waiting for STOPPED"
        else:
            started = pod.get("start_instance_ts")
            changed_timestamp = started is not None and started != previous_start
            if state in TRANSITION_STATES | {"STOPPED"}:
                observed_transition = True
            # A stopped baseline is already proof that the old instance was
            # not serving. Otherwise sites without timestamps must expose an
            # actual lifecycle transition after the request.
            new_instance = (changed_timestamp if started is not None else
                            observed_transition or previous_state == "STOPPED")
            if state in {"ERROR", "COMPLETE"} and (
                    noop or changed_timestamp or state != previous_state or observed_transition):
                raise DeploymentError(f"Pod entered {state}; inspect Tapis pod logs")
            if (state == "AVAILABLE" and pod.get("status_requested") in (None, "ON")
                    and (noop or new_instance)):
                container = pod.get("status_container") or {}
                if not isinstance(container, dict):
                    raise DeploymentError("Pod readiness response is invalid")
                if container.get("ready") is False:
                    last_problem = "pod readiness probe has not passed"
                elif pod.get("networking_live") is False:
                    last_problem = "pod networking is not live"
                elif clock() < deadline:
                    try:
                        status, body = client.request("GET", endpoint, timeout=min(10, deadline - clock()))
                    except DeploymentError:
                        last_problem = "health endpoint could not be reached"
                    else:
                        if status == 200 and matches_health(body, runtime, expected_sha):
                            # Health can race a deploy; read the pod again before
                            # reporting success, without repeating the mutation.
                            final = read()
                            if final.get("image") != image:
                                raise DeploymentError("Pod image changed while checking health")
                            final_container = final.get("status_container") or {}
                            same_instance = (started is None or final.get("start_instance_ts") == started)
                            if (final.get("status") == "AVAILABLE" and same_instance
                                    and final.get("status_requested") in (None, "ON")
                                    and isinstance(final_container, dict)
                                    and final_container.get("ready") is not False
                                    and final.get("networking_live") is not False):
                                log(f"Verified {pod_name} serves build {expected_sha}")
                                return {"action": action, "pod": pod_name, "image": image,
                                        "build_sha": expected_sha, "health_url": endpoint,
                                        "unchanged": noop}
                            last_problem = "pod instance changed while checking health"
                        else:
                            last_problem = "health response did not confirm the expected build"
            else:
                last_problem = "waiting for a new available instance"
        sleep(min(interval, max(0, deadline - clock())))
    raise DeploymentError(f"Lifecycle verification timed out after {timeout}s: {last_problem}. "
                          "Inspect pod state before requesting another action.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--action", required=True, choices=("start", "stop", "restart"))
    parser.add_argument("--base-url", default=os.environ.get("TAPIS_BASE_URL", "https://icicleai.tapis.io"))
    parser.add_argument("--tenant", default=os.environ.get("TAPIS_TENANT_ID", ""))
    parser.add_argument("--build-sha", default="")
    args = parser.parse_args()
    token = os.environ.get("TAPIS_PROVISION_TOKEN", "")
    if not token:
        parser.error("TAPIS_PROVISION_TOKEN is required in the environment")
    try:
        config = load_config(args.config)
        result = operate(HTTPClient(token, os.environ.get("HEALTHCHECK_TOKEN", "")), config,
                         args.action, base_url=args.base_url, tenant=args.tenant,
                         build_sha=args.build_sha)
    except MissingImageApproval as error:
        notify_missing_image(error, os.environ)
        print(f"Lifecycle action blocked: {error}", file=sys.stderr)
        return 1
    except (DeploymentError, ValueError, OSError) as error:
        print(f"Lifecycle action failed: {error}", file=sys.stderr)
        return 1
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(f"Verified `{result['action']}` for `{result['pod']}`.\n")
            if result.get("unchanged"):
                output.write("The requested state was already satisfied; no lifecycle action was sent.\n")
            if result.get("build_sha"):
                output.write(f"Build: `{result['build_sha']}`.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
