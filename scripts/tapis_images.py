"""Check tenant-scoped image approval and report missing approvals to Slack."""

import json
import re
import time
import urllib.parse
import urllib.request

from .deploy_tapis import (APIError, HTTPConnectionError, DeploymentError, NoRedirect,
                           api_result, checked_url)

# Tapis 26Q1's '*' applies to these tenants; '**' applies to all tenants.
# A matching row must still be inspected: GET /images may include rows added
# by the token owner even when those rows do not authorize the current tenant.
MAIN_TENANTS = frozenset({"tacc", "icicleai", "icicle", "dev", "astria", "a2cps", "scoped"})
REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")


def image_repository(image):
    # Current Tapis splits at the first colon. Explicit tags, without a registry
    # port or digest, avoid silently checking a different repository.
    if not isinstance(image, str) or image.count(":") != 1 or "@" in image or len(image) > 512:
        raise DeploymentError("Use an explicit repository:tag image; registry ports and digest references are unsupported")
    repository, tag = image.split(":")
    if not REPOSITORY.fullmatch(repository) or not TAG.fullmatch(tag):
        raise DeploymentError("Invalid tagged image reference")
    return repository


class MissingImageApproval(DeploymentError):
    def __init__(self, image, tenant, reason="repository-not-approved"):
        self.image = image
        self.repository = image_repository(image)
        self.tenant = tenant
        self.reason = reason
        super().__init__(f"Image approval required for {self.repository} in tenant {tenant}; "
                         "ask a platform administrator to approve the repository, then rerun")


def ensure_image_allowed(client, *, base_url, tenant, image, timeout=10,
                         clock=time.monotonic, sleep=time.sleep):
    repository = image_repository(image)
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", tenant):
        raise DeploymentError("Invalid Tapis tenant ID")
    base = checked_url(base_url).rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise DeploymentError("Tapis image approval checks require an HTTPS API URL")
    deadline = clock() + timeout
    for attempt in range(3):
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeploymentError("Image approval lookup timed out")
        try:
            entries = api_result(client, "GET", base + "/v3/pods/images", timeout=min(10, remaining))
            break
        except (APIError, HTTPConnectionError) as error:
            transient = isinstance(error, HTTPConnectionError) or error.status in {408, 429, 500, 502, 503, 504}
            if not transient or attempt == 2:
                raise
            sleep(min(2 ** attempt, max(0, deadline - clock())))
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise DeploymentError("Tapis image list has an unsupported response format")
    matched = False
    for entry in entries:
        if entry.get("image") != repository:
            continue
        matched = True
        tenants = entry.get("tenants")
        if not isinstance(tenants, list) or any(not isinstance(item, str) for item in tenants):
            raise DeploymentError("Tapis image approval entry lacks valid tenant policy")
        if "-" + tenant in tenants:
            continue
        if tenant in tenants or "**" in tenants or "*" in tenants and tenant in MAIN_TENANTS:
            return repository
    raise MissingImageApproval(image, tenant, "tenant-not-approved" if matched else "repository-not-approved")


def _run_url(env):
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[0-9]+", run_id)):
        return ""
    try:
        checked_url(server)
    except DeploymentError:
        return ""
    if urllib.parse.urlsplit(server).scheme != "https":
        return ""
    return f"{server}/{repository}/actions/runs/{run_id}"


def notify_missing_image(error, env, *, log=print, opener=None):
    """Best-effort notification; never mask the operation's original failure.

    A webhook request is sent once, without a Tapis token or a redirect. The
    workflow remains failed whether Slack is unavailable or unconfigured.
    """
    message = (f"Image approval required\nRepository: {error.repository}\n"
               f"Image: {error.image}\nTenant: {error.tenant}\n"
               "A platform administrator must approve this repository for the tenant. "
               "Rerun the workflow after approval.")
    run_url = _run_url(env)
    if run_url:
        message += "\nWorkflow: " + run_url
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as output:
                output.write(message.replace("\n", "\n\n") + "\n")
        except OSError:
            log("Could not write the missing-image approval summary")
    webhook = env.get("SLACK_PROVISIONING_WEBHOOK_URL", "")
    if not webhook:
        log("Slack notification not configured; image approval is still required")
        return False
    try:
        parts = urllib.parse.urlsplit(webhook)
        if (parts.scheme != "https" or parts.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}
                or parts.port not in (None, 443) or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(char.isspace() for char in webhook)
                or not re.fullmatch(r"/services/[A-Za-z0-9_/-]+", parts.path)):
            raise ValueError("Invalid Slack webhook")
        # Plain text blocks prevent image/user metadata from invoking mentions.
        payload = {"text": "Image approval required; see this message for the workflow link.",
                   "blocks": [{"type": "section", "text": {"type": "plain_text", "text": message}}]}
        request = urllib.request.Request(webhook, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with (opener or urllib.request.build_opener(NoRedirect())).open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Slack rejected the notification")
    except Exception:
        # Network exceptions contain the secret webhook URL. Never print them.
        log("Slack notification failed; image approval is still required. See the workflow summary.")
        return False
    log("Sent missing-image approval notification to Slack")
    return True
