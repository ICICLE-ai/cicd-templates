"""Verify debug activation, CLI output isolation, and HTTP secret boundaries."""

import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from scripts.deploy_tapis import HTTPClient, HTTPConnectionError, current_push
from scripts.logging_utils import configure_logging

ROOT = Path(__file__).resolve().parents[1]


class LoggingTests(unittest.TestCase):
    def test_debug_activation_and_banner(self):
        root = logging.getLogger()
        for debug, env, enabled in (
            (False, {}, False), (True, {}, True),
            (False, {"RUNNER_DEBUG": "1"}, True),
            (False, {"ACTIONS_STEP_DEBUG": "true"}, True),
            (False, {"RUNNER_DEBUG": "0", "ACTIONS_STEP_DEBUG": "false"}, False),
        ):
            with self.subTest(debug=debug, env=env):
                # Isolate basicConfig so unittest's existing handlers are preserved.
                handlers, level = root.handlers[:], root.level
                root.handlers = []
                try:
                    stream = io.StringIO()
                    with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(stream):
                        configure_logging(debug)
                        logging.getLogger("probe").debug("diagnostic detail")
                    self.assertEqual("DEBUG MODE ENABLED" in stream.getvalue(), enabled)
                    self.assertEqual("diagnostic detail" in stream.getvalue(), enabled)
                finally:
                    for handler in root.handlers:
                        handler.close()
                    root.handlers, root.level = handlers, level

    def test_requests_and_failures_are_visible_without_secrets(self):
        client = HTTPClient("api-secret", "health-secret")
        client.opener = mock.MagicMock()
        response = client.opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'response-secret'
        with self.assertLogs("scripts.deploy_tapis", level="DEBUG") as captured:
            client.request("PUT", "https://example.test/pod", api=True,
                           payload={"secret": "payload-secret"})
            client.request("GET", "https://example.test/health")
            client.opener.open.side_effect = urllib.error.HTTPError(
                "https://example.test/pod", 403, "error-secret", {}, None)
            self.assertEqual(client.request("GET", "https://example.test/pod", api=True), (403, b""))
            client.opener.open.side_effect = urllib.error.URLError("error-secret")
            with self.assertRaises(HTTPConnectionError):
                client.request("GET", "https://example.test/health")
        logs = "\n".join(captured.output)
        for text in ("Sending HTTP PUT https://example.test/pod", "-> 200", "-> 403", "failed (URLError"):
            self.assertIn(text, logs)
        for secret in ("api-secret", "health-secret", "response-secret", "payload-secret", "error-secret"):
            self.assertNotIn(secret, logs)

    def test_branch_request_is_logged_without_credentials_or_body(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps({"object": {"sha": "a" * 40}, "secret": "body-secret"}).encode()
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
               "GITHUB_REPOSITORY": "example/service", "GH_TOKEN": "github-secret"}
        with self.assertLogs("scripts.deploy_tapis", level="DEBUG") as captured:
            self.assertTrue(current_push(env, "a" * 40, opener=opener))
        logs = "\n".join(captured.output)
        self.assertIn("Sending HTTP GET https://api.github.com/repos/example/service/git/ref/heads/main", logs)
        self.assertIn("-> 200", logs)
        self.assertNotIn("github-secret", logs)
        self.assertNotIn("body-secret", logs)

    def test_json_stdout_survives_debug_logging(self):
        result = subprocess.run(
            [sys.executable, "scripts/validate_config.py", "icicle-service-v3-example.yaml", "--json", "--debug"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertIsInstance(json.loads(result.stdout), dict)
        self.assertIn("DEBUG MODE ENABLED", result.stderr)
        self.assertIn("Using schema version", result.stderr)
