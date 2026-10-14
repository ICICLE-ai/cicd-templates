"""Image approval is repository/tenant scoped; Slack never receives API secrets."""

import json
import tempfile
import unittest
from unittest import mock

from scripts.deploy_tapis import APIError, DeploymentError
from scripts.tapis_images import (MissingImageApproval, ensure_image_allowed,
                                  image_repository, notify_missing_image)

IMAGE = "ghcr.io/example/myservice:sha-" + "a" * 40 + "-123-1"
REPOSITORY = "ghcr.io/example/myservice"


class ImageApprovalTests(unittest.TestCase):
    def client(self, entries, status=200):
        client = mock.Mock()
        client.request.return_value = (status, json.dumps({"status": "success", "result": entries}).encode())
        return client

    def test_repository_tags_share_approval(self):
        for tag in ("release", "v2.0.0", "sha-" + "a" * 40 + "-123-1"):
            client = self.client([{"image": REPOSITORY, "tenants": ["icicleai"]}])
            self.assertEqual(ensure_image_allowed(client, base_url="https://icicleai.tapis.io",
                                                 tenant="icicleai", image=REPOSITORY + ":" + tag), REPOSITORY)
            self.assertEqual(client.request.call_args.args, ("GET", "https://icicleai.tapis.io/v3/pods/images"))
            self.assertTrue(client.request.call_args.kwargs["api"])

    def test_tenant_scope_and_explicit_denials(self):
        cases = [("icicleai", ["icicleai"], True), ("icicleai", ["*"], True),
                 ("custom", ["*"], False), ("custom", ["**"], True),
                 ("custom", ["custom"], True), ("icicleai", [], False),
                 ("icicleai", ["other"], False), ("icicleai", ["**", "-icicleai"], False),
                 ("icicleai", ["icicleai", "-icicleai"], False)]
        for tenant, tenants, allowed in cases:
            with self.subTest(tenant=tenant, tenants=tenants):
                # added_by must not override tenant policy.
                client = self.client([{"image": REPOSITORY, "tenants": tenants, "added_by": "automation"}])
                if allowed:
                    ensure_image_allowed(client, base_url="https://api.example", tenant=tenant, image=IMAGE)
                else:
                    with self.assertRaises(MissingImageApproval) as error:
                        ensure_image_allowed(client, base_url="https://api.example", tenant=tenant, image=IMAGE)
                    self.assertEqual(error.exception.reason, "tenant-not-approved")

    def test_no_namespace_prefix_matching(self):
        for entries in ([], [{"image": "ghcr.io/example/*", "tenants": ["**"]}],
                        [{"image": REPOSITORY + "other", "tenants": ["**"]}]):
            with self.assertRaises(MissingImageApproval):
                ensure_image_allowed(self.client(entries), base_url="https://api.example", tenant="icicleai", image=IMAGE)

    def test_bad_response_or_authorization_is_not_a_missing_approval(self):
        for entries in ({}, ["string"], [{"image": REPOSITORY}], [{"image": REPOSITORY, "tenants": "**"}]):
            with self.subTest(entries=entries), self.assertRaises(DeploymentError) as error:
                ensure_image_allowed(self.client(entries), base_url="https://api.example", tenant="icicleai", image=IMAGE)
            self.assertNotIsInstance(error.exception, MissingImageApproval)
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaises(APIError):
                ensure_image_allowed(self.client([], status), base_url="https://api.example", tenant="icicleai", image=IMAGE,
                                     sleep=lambda _: None)

    def test_transient_safe_reads_retry_and_remain_bounded(self):
        client = self.client([])
        client.request.side_effect = [(503, b""), (429, b""),
                                      (200, json.dumps({"status": "success", "result": [
                                          {"image": REPOSITORY, "tenants": ["icicleai"]}]}).encode())]
        sleep = mock.Mock()
        self.assertEqual(ensure_image_allowed(client, base_url="https://api.example", tenant="icicleai",
                                             image=IMAGE, sleep=sleep), REPOSITORY)
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        client.request.reset_mock()
        with self.assertRaises(DeploymentError):
            ensure_image_allowed(client, base_url="https://api.example", tenant="icicleai", image=IMAGE, timeout=0)
        client.request.assert_not_called()

    def test_unsupported_image_forms_fail_before_request(self):
        for image in ("repo", "repo@sha256:" + "a" * 64, "host:5000/repo:tag", "repo:",
                      "https://host/repo:tag", "Repo:tag", "repo:tag\n", "user:pass@host/repo:tag"):
            client = self.client([])
            with self.subTest(image=image), self.assertRaises(DeploymentError):
                ensure_image_allowed(client, base_url="https://api.example", tenant="icicleai", image=image)
            client.request.assert_not_called()


class SlackTests(unittest.TestCase):
    def setUp(self):
        self.error = MissingImageApproval(IMAGE, "icicleai")
        self.env = {"SLACK_PROVISIONING_WEBHOOK_URL": "https://hooks.slack.com/services/TEAM/CHANNEL/webhook-secret",
                    "TAPIS_TOKEN": "api-secret", "TAPIS_PROVISION_TOKEN": "admin-secret",
                    "HEALTHCHECK_TOKEN": "health-secret", "GITHUB_REPOSITORY": "example/platform",
                    "GITHUB_RUN_ID": "123", "GITHUB_SERVER_URL": "https://github.com"}

    def test_notification_contains_context_and_no_credentials(self):
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        log = mock.Mock()
        with tempfile.NamedTemporaryFile(mode="r+") as summary:
            env = {**self.env, "GITHUB_STEP_SUMMARY": summary.name}
            self.assertTrue(notify_missing_image(self.error, env, opener=opener, log=log))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(dict(request.header_items()), {"Content-type": "application/json"})
            body = request.data.decode()
            self.assertIn(REPOSITORY, body)
            self.assertIn("https://github.com/example/platform/actions/runs/123", body)
            self.assertEqual(json.loads(body)["blocks"][0]["text"]["type"], "plain_text")
            logs = summary.read() + body + str(log.call_args_list)
            for secret in ("api-secret", "admin-secret", "health-secret", "webhook-secret"):
                self.assertNotIn(secret, logs)
        opener.open.assert_called_once()

    def test_webhook_failure_not_retried_or_leaked_and_summary_survives(self):
        opener = mock.MagicMock()
        opener.open.side_effect = OSError(self.env["SLACK_PROVISIONING_WEBHOOK_URL"])
        log = mock.Mock()
        with tempfile.NamedTemporaryFile(mode="r+") as summary:
            self.assertFalse(notify_missing_image(self.error, {**self.env, "GITHUB_STEP_SUMMARY": summary.name}, opener=opener, log=log))
            self.assertIn("Image approval required", summary.read())
        opener.open.assert_called_once()
        self.assertNotIn("webhook-secret", str(log.call_args_list))

    def test_absent_or_invalid_webhook_never_sends(self):
        for webhook in ("", "https://evil.example/services/a/b/c", "http://hooks.slack.com/services/a/b/c",
                        "https://hooks.slack.com@evil.example/services/a/b/c", "https://hooks.slack.com/services/a?secret=x"):
            opener = mock.Mock()
            with self.subTest(webhook=webhook):
                self.assertFalse(notify_missing_image(self.error, {**self.env, "SLACK_PROVISIONING_WEBHOOK_URL": webhook}, opener=opener, log=lambda _: None))
                opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
