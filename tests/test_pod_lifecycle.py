"""Verify controlled lifecycle actions without contacting Tapis or Slack."""

import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock

from scripts import pod_lifecycle
from scripts.deploy_tapis import DeploymentError, HTTPConnectionError


SHA = "a" * 40
IMAGE = f"ghcr.io/example/service:sha-{SHA}-123-1"
CONFIG = {
    "schema-version": 1,
    "pod-name": "servicepod",
    "runtime-type": "python",
    "image": "ghcr.io/example/service:provisioned-long-ago",
    "build-sha": "b" * 40,
    "developers": ["developer"],
    "rollout-timeout": 300,
}
POD = {
    "image": IMAGE,
    "status": "AVAILABLE",
    "start_instance_ts": "old-instance",
    "status_container": {"ready": True},
    "networking": {"default": {"protocol": "http", "url": "service.pods.example"}},
    "environment_variables": {"PRIVATE_TOKEN": "never-print-me"},
}


def updated(**changes):
    return {**copy.deepcopy(POD), "start_instance_ts": "new-instance", **changes}


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class Client:
    def __init__(self, *, before=None, pre_reads=None, polls=None, health=None):
        self.before = copy.deepcopy(POD if before is None else before)
        self.pre_reads = list(pre_reads or [])
        self.polls = list(polls or [updated()])
        self.health = list(health or [(200, json.dumps({"status": "ok", "build_sha": SHA}).encode())])
        self.calls = []
        self.action = None
        self.mutation_error = None

    def request(self, method, url, *, api=False, payload=None, timeout=10):
        self.calls.append((method, url, api, payload, timeout))
        if not api:
            result = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            if isinstance(result, Exception):
                raise result
            return result
        action = url.rsplit("/", 1)[-1]
        if action in {"start", "stop", "restart"}:
            self.action = action
            if isinstance(self.mutation_error, Exception):
                raise self.mutation_error
            if self.mutation_error is not None:
                return self.mutation_error, b"secret API error body"
            result = {"status": "acknowledged"}
        elif self.action:
            result = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        else:
            result = self.pre_reads.pop(0) if self.pre_reads else self.before
        if isinstance(result, Exception):
            raise result
        if isinstance(result, int):
            return result, b"secret API error body"
        return 200, json.dumps({"status": "success", "result": result}).encode()

    @property
    def mutations(self):
        return [call for call in self.calls if call[1].rsplit("/", 1)[-1] in {"start", "stop", "restart"}]


class LifecycleTests(unittest.TestCase):
    def run_action(self, client, action="restart", *, config=None, **options):
        clock = Clock()
        with mock.patch.object(pod_lifecycle, "ensure_image_allowed") as approved:
            result = pod_lifecycle.operate(
                client, config or CONFIG, action,
                base_url="https://tapis.example", tenant="tenant", timeout=5, interval=1,
                clock=clock.now, sleep=clock.sleep, log=lambda _: None, **options,
            )
        return result, approved

    def test_start_and_restart_use_supported_endpoints_for_all_runtimes(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            for action in ("start", "restart"):
                with self.subTest(runtime=runtime, action=action):
                    before = {**POD, "status": "STOPPED"} if action == "start" else POD
                    health = [(200, SHA.encode())] if runtime == "node-frontend" else None
                    client = Client(before=before, health=health)
                    result, approved = self.run_action(client, action, config={**CONFIG, "runtime-type": runtime})
                    self.assertEqual(result["build_sha"], SHA)
                    self.assertEqual(result["image"], IMAGE)
                    self.assertFalse(result["unchanged"])
                    self.assertEqual(len(client.mutations), 1)
                    self.assertEqual(client.mutations[0][:4], (
                        "GET", f"https://tapis.example/v3/pods/servicepod/{action}", True, None))
                    approved.assert_called_once_with(client, base_url="https://tapis.example", tenant="tenant", image=IMAGE, timeout=5)
                    suffix = "/healthcheck" if runtime == "node-frontend" else "/health"
                    self.assertEqual(result["health_url"], "https://service.pods.example" + suffix)

    def test_stop_does_not_require_image_approval_sha_or_health(self):
        for image in ("unapproved/image:latest", None):
            with self.subTest(image=image):
                before = {**POD, "image": image, "networking": {}, "status": "AVAILABLE"}
                client = Client(before=before, polls=[{**before, "status": "STOPPED"}])
                result, approved = self.run_action(client, "stop", config={**CONFIG, "health-url": "bad"})
                approved.assert_not_called()
                self.assertEqual(result["action"], "stop")
                self.assertEqual(len(client.mutations), 1)
                self.assertTrue(all(call[2] for call in client.calls))

    def test_already_stopped_is_noop(self):
        client = Client(before={**POD, "status": "STOPPED", "networking": {}})
        result, approved = self.run_action(client, "stop")
        self.assertTrue(result["unchanged"])
        self.assertEqual(client.mutations, [])
        approved.assert_not_called()

    def test_already_available_start_checks_health_without_restarting(self):
        client = Client()
        result, approved = self.run_action(client, "start")
        self.assertTrue(result["unchanged"])
        self.assertEqual(client.mutations, [])
        self.assertTrue(any(not call[2] for call in client.calls))
        approved.assert_called_once()

    def test_unhealthy_already_available_start_fails_without_restart(self):
        client = Client(health=[(200, b'{"status":"ok","build_sha":"wrong"}')])
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_action(client, "start")
        self.assertEqual(client.mutations, [])

    def test_live_tag_overrides_manifest_build_and_explicit_mismatch_fails(self):
        result, _ = self.run_action(Client())
        self.assertEqual(result["build_sha"], SHA)
        client = Client()
        with self.assertRaisesRegex(DeploymentError, "does not match"):
            self.run_action(client, build_sha="c" * 40)
        self.assertEqual(client.mutations, [])

    def test_nonstandard_image_requires_explicit_build_sha(self):
        before = {**POD, "image": "ghcr.io/example/service:release"}
        client = Client(before=before)
        with self.assertRaisesRegex(DeploymentError, "provide --build-sha"):
            self.run_action(client)
        self.assertEqual(client.mutations, [])
        client = Client(before=before, polls=[updated(image=before["image"])])
        result, _ = self.run_action(client, build_sha=SHA)
        self.assertEqual(result["build_sha"], SHA)

    def test_missing_approval_prevents_mutation(self):
        client = Client()
        clock = Clock()
        with mock.patch.object(pod_lifecycle, "ensure_image_allowed", side_effect=DeploymentError("approval required")):
            with self.assertRaisesRegex(DeploymentError, "approval required"):
                pod_lifecycle.operate(client, CONFIG, "restart", base_url="https://tapis.example", tenant="tenant",
                                      clock=clock.now, sleep=clock.sleep)
        self.assertEqual(client.mutations, [])

    def test_mutation_is_never_retried(self):
        for action in ("start", "stop", "restart"):
            for failure in (503, HTTPConnectionError("HTTP connection failed")):
                with self.subTest(action=action, failure=failure):
                    before = {**POD, "status": "STOPPED"} if action == "start" else POD
                    client = Client(before=before)
                    client.mutation_error = failure
                    with self.assertRaises(DeploymentError):
                        self.run_action(client, action)
                    self.assertEqual(len(client.mutations), 1)

    def test_transient_safe_reads_retry_without_duplicate_mutation(self):
        for failure in (503, HTTPConnectionError("HTTP connection failed")):
            with self.subTest(failure=failure):
                client = Client(pre_reads=[failure, POD, POD], polls=[failure, updated()])
                result, _ = self.run_action(client)
                self.assertEqual(result["build_sha"], SHA)
                self.assertEqual(len(client.mutations), 1)

    def test_read_permission_failure_is_not_retried(self):
        client = Client(pre_reads=[403])
        with self.assertRaisesRegex(DeploymentError, "HTTP 403"):
            self.run_action(client)
        self.assertEqual(len(client.calls), 1)

    def test_stale_available_instance_never_satisfies_restart(self):
        for timestamp in ("old-instance", None):
            with self.subTest(timestamp=timestamp):
                before = {**POD, "start_instance_ts": timestamp}
                client = Client(before=before, polls=[before])
                with self.assertRaisesRegex(DeploymentError, "timed out"):
                    self.run_action(client)
                self.assertFalse(any(not call[2] for call in client.calls))
                self.assertEqual(len(client.mutations), 1)

    def test_no_timestamp_requires_observed_transition(self):
        before = {**POD, "start_instance_ts": None}
        client = Client(before=before, polls=[{**before, "status": "SPAWNING"}, before])
        result, _ = self.run_action(client)
        self.assertEqual(result["build_sha"], SHA)

    def test_stopped_start_can_verify_without_timestamp(self):
        before = {**POD, "status": "STOPPED", "start_instance_ts": None}
        client = Client(before=before, polls=[{**POD, "start_instance_ts": None}])
        result, _ = self.run_action(client, "start")
        self.assertEqual(result["build_sha"], SHA)

    def test_pending_operation_does_not_receive_another_start_or_restart(self):
        for action in ("start", "restart"):
            client = Client(before={**POD, "status": "SPAWNING"})
            with self.assertRaisesRegex(DeploymentError, "operation in progress"):
                self.run_action(client, action)
            self.assertEqual(client.mutations, [])

    def test_pending_requested_state_is_not_mistaken_for_a_stable_noop(self):
        for status, requested in (("AVAILABLE", "OFF"), ("AVAILABLE", "RESTART"), ("STOPPED", "ON")):
            for action in ("start", "restart"):
                with self.subTest(status=status, requested=requested, action=action):
                    client = Client(before={**POD, "status": status, "status_requested": requested})
                    with self.assertRaisesRegex(DeploymentError, "operation in progress"):
                        self.run_action(client, action)
                    self.assertEqual(client.mutations, [])

    def test_stop_cancels_a_pending_start_instead_of_reporting_stopped_noop(self):
        client = Client(before={**POD, "status": "STOPPED", "status_requested": "ON"},
                        polls=[updated(status="STOPPED", status_requested="OFF")])
        result, _ = self.run_action(client, "stop")
        self.assertFalse(result["unchanged"])
        self.assertEqual(len(client.mutations), 1)

    def test_concurrent_image_change_aborts_before_or_after_mutation(self):
        changed = updated(image="ghcr.io/example/service:changed")
        for client, expected_mutations in (
                (Client(pre_reads=[POD, changed]), 0),
                (Client(polls=[changed]), 1),
                (Client(polls=[updated(), changed]), 1)):
            with self.subTest(expected_mutations=expected_mutations, client=client):
                with self.assertRaisesRegex(DeploymentError, "image changed"):
                    self.run_action(client)
                self.assertEqual(len(client.mutations), expected_mutations)

    def test_readiness_and_health_must_both_pass(self):
        client = Client(polls=[updated(status_container={"ready": False})])
        with self.assertRaisesRegex(DeploymentError, "readiness"):
            self.run_action(client)
        self.assertFalse(any(not call[2] for call in client.calls))
        client = Client(health=[(200, b'{"status":"error","build_sha":"' + SHA.encode() + b'"}')])
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_action(client)

    def test_networking_must_be_live_before_accepting_health(self):
        client = Client(polls=[updated(networking_live=False)])
        with self.assertRaisesRegex(DeploymentError, "networking is not live"):
            self.run_action(client)
        self.assertFalse(any(not call[2] for call in client.calls))

    def test_every_action_rejects_plaintext_api_before_sending_credentials(self):
        for action in ("start", "stop", "restart"):
            with self.subTest(action=action):
                client = Client()
                with self.assertRaisesRegex(DeploymentError, "HTTPS"):
                    pod_lifecycle.operate(client, CONFIG, action, base_url="http://tapis.example", tenant="tenant")
                self.assertEqual(client.calls, [])

    def test_stop_waits_until_stopped_and_has_a_bounded_timeout(self):
        client = Client(polls=[updated(status="SHUTTING_DOWN")])
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_action(client, "stop")
        self.assertEqual(len(client.mutations), 1)

    def test_failed_new_instance_fails_fast(self):
        client = Client(polls=[updated(status="ERROR")])
        with self.assertRaisesRegex(DeploymentError, "entered ERROR"):
            self.run_action(client)
        self.assertFalse(any(not call[2] for call in client.calls))

    def test_logging_never_includes_api_bodies_env_or_health_errors(self):
        clock = Clock()
        logs = []
        client = Client(health=[HTTPConnectionError("PRIVATE-health-token")])
        with mock.patch.object(pod_lifecycle, "ensure_image_allowed"):
            with self.assertRaises(DeploymentError) as error:
                pod_lifecycle.operate(client, CONFIG, "restart", base_url="https://tapis.example", tenant="tenant",
                                      timeout=3, interval=1, clock=clock.now, sleep=clock.sleep, log=logs.append)
        output = " ".join(logs) + str(error.exception)
        for sensitive in ("PRIVATE", "never-print-me", "environment_variables", "secret API"):
            self.assertNotIn(sensitive, output)

    def test_cli_uses_only_provisioning_token_and_optional_health_token(self):
        env = {"TAPIS_PROVISION_TOKEN": "private-provision-token", "HEALTHCHECK_TOKEN": "private-health-token",
               "TAPIS_TENANT_ID": "anotherTenant"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.argv", ["pod_lifecycle.py", "--config", "pods/service.yaml", "--action", "stop"]), \
                mock.patch.object(pod_lifecycle, "load_config", return_value=CONFIG), \
                mock.patch.object(pod_lifecycle, "HTTPClient") as factory, \
                mock.patch.object(pod_lifecycle, "operate", return_value={"action": "stop", "pod": "servicepod"}) as operate:
            self.assertEqual(pod_lifecycle.main(), 0)
            factory.assert_called_once_with("private-provision-token", "private-health-token")
            self.assertEqual(operate.call_args.args[1:3], (CONFIG, "stop"))
            self.assertEqual(operate.call_args.kwargs["tenant"], "anotherTenant")

    def test_cli_does_not_accept_ordinary_deployment_token(self):
        with mock.patch.dict(os.environ, {"TAPIS_TOKEN": "wrong-token"}, clear=True), \
                mock.patch("sys.argv", ["pod_lifecycle.py", "--config", "pods/service.yaml", "--action", "stop"]), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                pod_lifecycle.main()
        self.assertIn("TAPIS_PROVISION_TOKEN is required", output.getvalue())
        self.assertNotIn("wrong-token", output.getvalue())

    def test_cli_notifies_missing_approval_once_and_remains_failed(self):
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                error = pod_lifecycle.MissingImageApproval(IMAGE, "tenant")
                with mock.patch.dict(os.environ, {"TAPIS_PROVISION_TOKEN": "private-token"}, clear=True), \
                        mock.patch("sys.argv", ["pod_lifecycle.py", "--config", "pods/service.yaml", "--action", "restart"]), \
                        mock.patch.object(pod_lifecycle, "load_config", return_value=CONFIG), \
                        mock.patch.object(pod_lifecycle, "operate", side_effect=error), \
                        mock.patch.object(pod_lifecycle, "notify_missing_image", return_value=delivered) as notify, \
                        contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(pod_lifecycle.main(), 1)
                    notify.assert_called_once_with(error, os.environ)
                self.assertIn("Lifecycle action blocked", output.getvalue())
                self.assertNotIn("private-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
