"""Update one existing Tapis pod and verify its new instance and build SHA."""

import argparse
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .logging_utils import add_debug_argument, configure_logging
else:
    from logging_utils import add_debug_argument, configure_logging

logger = logging.getLogger(__name__)


class DeploymentError(RuntimeError):
    pass


class HTTPConnectionError(DeploymentError):
    pass


class APIError(DeploymentError):
    def __init__(self, method, status):
        self.status = status
        super().__init__(f"Tapis {method} failed with HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward either deployment or health credentials to a redirect.
        return None


class HTTPClient:
    def __init__(self, token, health_token=""):
        self.token = token
        self.health_token = health_token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, url, *, payload=None, api=False, timeout=10):
        checked_url(url)
        headers = {"Accept": "application/json" if api else "*/*"}
        if api:
            headers["X-Tapis-Token"] = self.token
        else:
            headers["Cache-Control"] = "no-cache, no-store"
            if self.health_token:
                headers["Authorization"] = f"Bearer {self.health_token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        started = time.monotonic()
        logger.info("Sending HTTP %s %s", method, url)
        logger.debug("Request timeout: %.2fs; JSON payload present: %s", timeout, payload is not None)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                logger.info("HTTP %s %s -> %s (%.3fs)", method, url,
                            response.status, time.monotonic() - started)
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise DeploymentError("Response exceeded the 1 MiB limit")
                return response.status, body
        except urllib.error.HTTPError as error:
            # Do not log API bodies: pod definitions may contain secrets.
            logger.info("HTTP %s %s -> %s (%.3fs)", method, url,
                        error.code, time.monotonic() - started)
            return error.code, b""
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            logger.warning("HTTP %s %s failed (%s; %.3fs)", method, url,
                           type(error).__name__, time.monotonic() - started)
            raise HTTPConnectionError("HTTP connection failed") from error


def checked_url(value):
    if not isinstance(value, str) or not value:
        raise DeploymentError("Health/API URLs must be nonempty strings")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise DeploymentError("Invalid health/API URL or port") from error
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in value or "#" in value
            or (port is not None and not 1 <= port <= 65535)
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise DeploymentError("Health/API URLs must be HTTP(S), without credentials, query, or fragment")
    return value


def health_url(pod, runtime, override=""):
    if override:
        return checked_url(override)
    networking = pod.get("networking") or {}
    if not isinstance(networking, dict):
        raise DeploymentError("Pod networking must be an object; set health-url to override discovery")
    candidates = {}
    for name, entry in networking.items():
        if not isinstance(entry, dict) or entry.get("protocol", "http") != "http":
            continue
        url = entry.get("url")
        if url:
            if not isinstance(url, str):
                raise DeploymentError("Pod networking HTTP URLs must be strings")
            candidates[name] = url
    if "default" in candidates:
        base = candidates["default"]
    elif len(candidates) == 1:
        base = next(iter(candidates.values()))
    else:
        raise DeploymentError("Set health-url: pod networking has no unambiguous HTTP endpoint")
    if "://" not in base:
        base = "https://" + base
    checked_url(base)
    path = "/healthcheck" if runtime == "node-frontend" else "/health"
    return base.rstrip("/") + path


def api_result(client, method, url, payload=None, timeout=10):
    status, body = client.request(method, url, payload=payload, api=True, timeout=timeout)
    if not 200 <= status < 300:
        raise APIError(method, status)
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise DeploymentError("Tapis returned invalid JSON") from error
    if not isinstance(envelope, dict) or envelope.get("status") != "success":
        raise DeploymentError("Tapis did not report success")
    return envelope.get("result")


def get_pod(client, url, timeout=10):
    pod = api_result(client, "GET", url, timeout=timeout)
    if not isinstance(pod, dict):
        raise DeploymentError("Tapis did not return a pod definition")
    return pod


def read_pod(client, url, *, deadline, clock, sleep, attempts=3):
    """Retry safe pod reads only, within the deployment's overall deadline."""
    for attempt in range(attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeploymentError("Deployment deadline expired while reading pod")
        try:
            return get_pod(client, url, timeout=min(10, remaining))
        except (APIError, HTTPConnectionError) as error:
            transient = (isinstance(error, HTTPConnectionError)
                         or error.status in {408, 429, 500, 502, 503, 504})
            if not transient or attempt == attempts - 1:
                raise
            delay = min(2 ** attempt, max(0, deadline - clock()))
            logger.debug("Pod read attempt %s/%s failed: %s; retrying in %.2fs",
                         attempt + 1, attempts, error, delay)
            sleep(delay)


def current_push(env, build_sha, *, opener=None):
    """Manual dispatch can roll back; automatic branch pushes must still be HEAD."""
    if env.get("GITHUB_EVENT_NAME") != "push":
        return True
    ref = env.get("GITHUB_REF", "")
    if not ref.startswith("refs/heads/"):
        return True
    repository = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise DeploymentError("Branch freshness check requires GITHUB_REPOSITORY and GH_TOKEN")
    base = checked_url(env.get("GITHUB_API_URL", "https://api.github.com"))
    if not base.startswith("https://"):
        raise DeploymentError("GitHub API must use HTTPS")
    branch = urllib.parse.quote(ref[len("refs/heads/"):], safe="")
    url = f"{base.rstrip('/')}/repos/{repository}/git/ref/heads/{branch}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    started = time.monotonic()
    logger.info("Sending HTTP GET %s (branch freshness check)", url)
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(request, timeout=10) as response:
            logger.info("HTTP GET %s -> %s (%.3fs)", url, response.status,
                        time.monotonic() - started)
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("oversized response")
            sha = json.loads(body)["object"]["sha"]
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise ValueError("invalid branch SHA")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
            ValueError, KeyError, TypeError) as error:
        logger.warning("Branch freshness request failed (%s; HTTP %s; %.3fs)",
                       type(error).__name__, getattr(error, "code", "unavailable"),
                       time.monotonic() - started)
        # Never print credentials, response bodies, or request headers.
        raise DeploymentError("Could not verify current branch HEAD; pod was not changed") from error
    logger.debug("Branch HEAD matches requested build: %s", sha == build_sha)
    return sha == build_sha


def matches_health(body, runtime, expected_sha):
    if runtime == "node-frontend":
        try:
            return body.decode("utf-8").strip() == expected_sha
        except UnicodeDecodeError:
            return False
    try:
        health = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(health, dict) and health.get("build_sha") == expected_sha
            and health.get("status") == "ok")


def deploy(client, *, base_url, pod_name, image, runtime, build_sha,
           override_url="", timeout=300, interval=5, clock=time.monotonic,
           sleep=time.sleep, log=logger.info, should_deploy=lambda: True):
    """Mutate only image; keep all deployment-owned environment/network fields."""
    if not re.fullmatch(r"[a-z][a-z0-9]{3,63}", pod_name):
        raise DeploymentError("Invalid pod-name")
    if runtime not in {"python", "node-backend", "node-frontend"}:
        raise DeploymentError("Unsupported runtime")
    if not re.fullmatch(r"[0-9a-f]{40}", build_sha):
        raise DeploymentError("Expected a full lowercase Git build SHA")
    if not image or any(char.isspace() for char in image):
        raise DeploymentError("Invalid image reference")
    pod_url = checked_url(base_url).rstrip("/") + "/v3/pods/" + pod_name
    deadline = clock() + timeout
    before = read_pod(client, pod_url, deadline=deadline, clock=clock, sleep=sleep)
    endpoint = health_url(before, runtime, override_url)
    logger.debug("Rollout timeout: %ss; poll interval: %ss; health endpoint: %s",
                 timeout, interval, endpoint)
    previous_start = before.get("start_instance_ts")
    previous_state = before.get("status", "UNKNOWN")
    previous_image = before.get("image", "")
    # Recheck after the initial read, immediately before the first mutation.
    if not should_deploy():
        log("Skipped superseded automatic push: the branch now has a newer HEAD; pod was not changed")
        return {"skipped": True}
    if clock() >= deadline:
        raise DeploymentError("Deployment deadline expired before updating pod")
    log(f"Deploying {image} to {pod_name}; previous image: {previous_image}")
    # Minimal update preserves environment_variables, networking, commands,
    # volumes, resources, and all other settings owned by the platform team.
    api_result(client, "PUT", pod_url, {"image": image}, timeout=min(10, deadline - clock()))
    configured = read_pod(client, pod_url, deadline=deadline, clock=clock, sleep=sleep)
    if configured.get("image") != image:
        raise DeploymentError("Tapis did not persist the requested image; restart was not requested")
    # Deliberately do not retry this mutation: a lost response must not trigger
    # repeated restarts. A subsequent workflow run can safely deploy again.
    if clock() >= deadline:
        raise DeploymentError("Deployment deadline expired before restart; requested image remains configured")
    api_result(client, "GET", pod_url + "/restart", timeout=min(10, deadline - clock()))
    # A restart acknowledgement is not evidence that the serving instance
    # changed. Sites without timestamps must expose lifecycle progress in a
    # subsequent pod poll, even when the existing instance serves the same SHA.
    observed_restart = False
    observed_poll_transition = False
    last_state = None
    last_problem = "waiting for a new pod instance"
    while clock() < deadline:
        remaining = min(10, deadline - clock())
        if remaining <= 0:
            break
        try:
            pod = get_pod(client, pod_url, timeout=remaining)
        except DeploymentError as error:
            if not (isinstance(error, HTTPConnectionError)
                    or isinstance(error, APIError) and error.status in {408, 429, 500, 502, 503, 504}):
                raise
            last_problem = str(error)
        else:
            state = pod.get("status", "UNKNOWN")
            if state != last_state:
                log(f"Pod status: {state}")
                last_state = state
            if pod.get("image") != image:
                raise DeploymentError("Pod image changed during rollout; another deployment may be running")
            started = pod.get("start_instance_ts")
            instance_changed = started is not None and started != previous_start
            # The first status reads can still describe the failed instance
            # that prompted this deploy. Only fail once a poll shows progress;
            # a restart acknowledgement alone does not prove it has happened.
            if state != previous_state:
                observed_poll_transition = True
            if state in {"ERROR", "COMPLETE"} and (instance_changed or observed_poll_transition):
                raise DeploymentError(f"Pod entered {state}; inspect Tapis pod logs")
            if state in {"RESTARTING", "SHUTTING_DOWN", "DELETING", "STOPPED", "REQUESTED", "SPAWNING"}:
                observed_restart = True
            # Prefer the instance timestamp. If older sites omit it, require
            # an observed lifecycle transition; never accept a stale AVAILABLE.
            new_instance = (started != previous_start if started is not None
                            else observed_restart)
            if state == "AVAILABLE" and new_instance:
                container = pod.get("status_container") or {}
                if container.get("ready") is False:
                    last_problem = "pod readiness probe has not passed"
                else:
                    remaining = min(10, deadline - clock())
                    if remaining <= 0:
                        break
                    try:
                        status, body = client.request("GET", endpoint, timeout=remaining)
                    except DeploymentError as error:
                        last_problem = str(error)
                    else:
                        if status == 200 and matches_health(body, runtime, build_sha):
                            log(f"Rollout verified: {pod_name} serves build {build_sha}")
                            return {"image": image, "previous_image": previous_image,
                                    "health_url": endpoint, "build_sha": build_sha}
                        last_problem = f"health HTTP {status}; expected build SHA not confirmed"
            else:
                last_problem = f"pod {state}; waiting for a new instance"
        logger.debug("Rollout pending: %s; %.2fs remaining", last_problem, max(0, deadline - clock()))
        sleep(min(interval, max(0, deadline - clock())))
    raise DeploymentError(f"Rollout timed out after {timeout}s: {last_problem}. "
                          "The requested image is still configured; inspect pod logs before retrying or rolling back.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("TAPIS_BASE_URL", "https://icicleai.tapis.io"))
    parser.add_argument("--pod", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--health-url", default="")
    parser.add_argument("--timeout", type=int, default=300)
    add_debug_argument(parser)
    args = parser.parse_args()
    configure_logging(args.debug)
    token = os.environ.get("TAPIS_TOKEN", "")
    if not token:
        parser.error("TAPIS_TOKEN is required in the environment")
    if not 30 <= args.timeout <= 1800:
        parser.error("--timeout must be 30–1800 seconds")
    try:
        result = deploy(HTTPClient(token, os.environ.get("HEALTHCHECK_TOKEN", "")),
                        base_url=args.base_url, pod_name=args.pod, image=args.image,
                        runtime=args.runtime, build_sha=args.build_sha,
                        override_url=args.health_url, timeout=args.timeout,
                        should_deploy=lambda: current_push(os.environ, args.build_sha))
    except (DeploymentError, ValueError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            if result.get("skipped"):
                output.write("Skipped superseded automatic push: the branch now has a newer HEAD. "
                             "The pod was not changed by this run.\n")
                return 0
            output.write(f"Verified `{args.pod}` at build `{args.build_sha}`.\n\n"
                         f"Image: `{result['image']}`\n\n"
                         f"Previous image: `{result['previous_image']}`\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
