"""Exercise deployment decisions and HTTP credential boundaries without Tapis."""

import contextlib
import copy
import http.server
import json
import threading
import tempfile
import unittest
from unittest import mock

from scripts.deploy_tapis import (
    DeploymentError,
    HTTPClient,
    deploy,
    health_url,
    matches_health,
    current_push,
    read_pod,
    HTTPConnectionError,
    main,
)

SHA = "a" * 40
IMAGE = "ghcr.io/example/service@sha256:" + "b" * 64
POD = {
    "image": "ghcr.io/example/service:old",
    "status": "AVAILABLE",
    "start_instance_ts": "old-instance",
    "environment_variables": {"BACKEND_URL": "https://backend.example"},
    "networking": {"default": {"protocol": "http", "url": "service.pods.example"}},
    "resources": {"cpu_limit": 1000},
}


def new_pod(**changes):
    return {**copy.deepcopy(POD), "image": IMAGE, "start_instance_ts": "new-instance", **changes}


class FakeClock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class FakeClient:
    def __init__(self, polls=None, health=None, before=None):
        self.before = copy.deepcopy(before or POD)
        self.configured = copy.deepcopy(self.before)
        self.polls = list(polls or [new_pod()])
        self.health = list(health or [(200, json.dumps({"status": "ok", "build_sha": SHA}).encode())])
        self.restarted = False
        self.calls = []
        self.put_status = 200
        self.persist_image = True
        self.restart_result = {"status": "RESTARTING"}

    def request(self, method, url, *, payload=None, api=False, timeout=10):
        self.calls.append((method, url, payload, api))
        if not api:
            return self.health.pop(0) if len(self.health) > 1 else self.health[0]
        if method == "PUT":
            if self.persist_image:
                self.configured.update(payload)
            return self.put_status, json.dumps({"status": "success", "result": self.configured}).encode()
        if url.endswith("/restart"):
            self.restarted = True
            result = self.restart_result
        elif self.restarted:
            result = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            if isinstance(result, int):
                return result, b""
        else:
            result = self.configured
        return 200, json.dumps({"status": "success", "result": result}).encode()


class DeploymentTests(unittest.TestCase):
    def run_deploy(self, client, runtime="python", **kwargs):
        clock = FakeClock()
        return deploy(client, base_url="https://tapis.example", pod_name="servicepod",
                      image=IMAGE, runtime=runtime, build_sha=SHA, timeout=3, interval=1,
                      clock=clock.now, sleep=clock.sleep, log=kwargs.pop("log", lambda _: None), **kwargs)

    def test_updates_only_image_before_restart_and_verifies_each_runtime(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            with self.subTest(runtime=runtime):
                health = [(200, SHA.encode())] if runtime == "node-frontend" else None
                client = FakeClient(health=health)
                result = self.run_deploy(client, runtime)
                self.assertEqual(result["image"], IMAGE)
                self.assertEqual([call[0] for call in client.calls[:4]], ["GET", "PUT", "GET", "GET"])
                self.assertEqual(client.calls[1][2], {"image": IMAGE})
                self.assertEqual(client.calls[1][1], "https://tapis.example/v3/pods/servicepod")
                self.assertEqual(client.calls[3][1], "https://tapis.example/v3/pods/servicepod/restart")
                for key in ("environment_variables", "networking", "resources"):
                    self.assertEqual(client.configured[key], POD[key])
                suffix = "/healthcheck" if runtime == "node-frontend" else "/health"
                self.assertEqual(result["health_url"], "https://service.pods.example" + suffix)

    def test_same_digest_skips_put_but_still_restarts_and_requires_current_build(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            old_health = (b"old" if runtime == "node-frontend" else
                          b'{"status":"ok","build_sha":"old"}')
            current_health = (SHA.encode() if runtime == "node-frontend" else
                              json.dumps({"status": "ok", "build_sha": SHA}).encode())
            with self.subTest(runtime=runtime, current_build=True):
                client = FakeClient(before=dict(POD, image=IMAGE),
                                    health=[(200, old_health), (200, current_health)])
                messages = []
                client.put_status = 400  # Any attempted update would fail.
                result = self.run_deploy(client, runtime, log=messages.append)
                self.assertTrue(any("skipping PUT" in message for message in messages))
                self.assertEqual(result["previous_image"], IMAGE)
                self.assertEqual(result["image"], IMAGE)
                self.assertEqual(result["build_sha"], SHA)
                self.assertEqual(sum(call[0] == "PUT" for call in client.calls), 0)
                self.assertEqual(sum(call[1].endswith("/restart") for call in client.calls), 1)
                self.assertEqual(sum(not call[3] for call in client.calls), 2)
            with self.subTest(runtime=runtime, current_build=False):
                client = FakeClient(before=dict(POD, image=IMAGE), health=[(200, old_health)])
                with self.assertRaisesRegex(DeploymentError, "expected build SHA not confirmed"):
                    self.run_deploy(client, runtime)
                self.assertTrue(client.restarted)

    def test_superseded_push_skips_immediately_before_mutation(self):
        client = FakeClient()
        observed = []
        def guard():
            observed.extend(client.calls)
            return False
        self.assertEqual(self.run_deploy(client, should_deploy=guard), {"skipped": True})
        self.assertEqual([call[0] for call in observed], ["GET"])
        self.assertEqual(client.calls, observed)
        self.assertFalse(client.restarted)

    def test_transient_initial_and_readback_gets_retry_without_repeating_mutations(self):
        for failure_index in (0, 2):
            for failure in (503, HTTPConnectionError("HTTP connection failed")):
                with self.subTest(failure_index=failure_index, failure=failure):
                    client = FakeClient()
                    original = client.request
                    count = 0
                    def flaky(method, url, **kwargs):
                        nonlocal count
                        count += 1
                        if count == failure_index + 1:
                            self.assertEqual(method, "GET")
                            self.assertFalse(url.endswith("/restart"))
                            if isinstance(failure, Exception):
                                raise failure
                            return failure, b""
                        return original(method, url, **kwargs)
                    client.request = flaky
                    self.run_deploy(client)
                    self.assertEqual(sum(call[0] == "PUT" for call in client.calls), 1)
                    self.assertEqual(sum(call[1].endswith("/restart") for call in client.calls), 1)

    def test_mutations_are_not_retried_on_transient_failure(self):
        for mutation in ("PUT", "restart"):
            with self.subTest(mutation=mutation):
                client = FakeClient()
                original = client.request
                calls = []
                def failing(method, url, **kwargs):
                    if method == mutation or mutation == "restart" and url.endswith("/restart"):
                        calls.append((method, url))
                        return 503, b""
                    return original(method, url, **kwargs)
                client.request = failing
                with self.assertRaisesRegex(DeploymentError, "HTTP 503"):
                    self.run_deploy(client)
                self.assertEqual(len(calls), 1)

    def test_safe_reads_fail_fast_for_auth_missing_pod_and_invalid_responses(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                client = mock.Mock()
                client.request.return_value = status, b""
                clock = FakeClock()
                with self.assertRaisesRegex(DeploymentError, f"HTTP {status}"):
                    read_pod(client, "https://tapis.example/pod", deadline=30,
                             clock=clock.now, sleep=clock.sleep)
                self.assertEqual(client.request.call_count, 1)
        for body in (b"invalid-json", b'{"status":"success","result":[]}', b'{"status":"error"}'):
            with self.subTest(body=body):
                client = mock.Mock()
                client.request.return_value = 200, body
                clock = FakeClock()
                with self.assertRaises(DeploymentError):
                    read_pod(client, "https://tapis.example/pod", deadline=30,
                             clock=clock.now, sleep=clock.sleep)
                self.assertEqual(client.request.call_count, 1)

    def test_safe_read_retries_are_bounded_and_respect_deadline(self):
        for deadline, expected_calls in ((30, 3), (1, 1)):
            with self.subTest(deadline=deadline):
                clock = FakeClock()
                client = mock.Mock()
                client.request.return_value = 503, b""
                with self.assertRaises(DeploymentError):
                    read_pod(client, "https://tapis.example/pod", deadline=deadline,
                             clock=clock.now, sleep=clock.sleep)
                self.assertEqual(client.request.call_count, expected_calls)
                self.assertLessEqual(clock.value, deadline)

    def test_deadline_prevents_mutation_or_health_probe_after_budget_expires(self):
        client = FakeClient()
        clock = FakeClock()
        def slow_guard():
            clock.sleep(3)
            return True
        with self.assertRaisesRegex(DeploymentError, 'deadline expired before updating'):
            deploy(client, base_url='https://tapis.example', pod_name='servicepod',
                   image=IMAGE, runtime='python', build_sha=SHA, timeout=3,
                   clock=clock.now, sleep=clock.sleep, should_deploy=slow_guard)
        self.assertEqual([call[0] for call in client.calls], ['GET'])
        client = FakeClient()
        clock = FakeClock()
        original = client.request
        def slow_poll(method, url, **kwargs):
            if client.restarted:
                clock.sleep(3)
            return original(method, url, **kwargs)
        client.request = slow_poll
        with self.assertRaisesRegex(DeploymentError, 'timed out'):
            deploy(client, base_url='https://tapis.example', pod_name='servicepod',
                   image=IMAGE, runtime='python', build_sha=SHA, timeout=3,
                   clock=clock.now, sleep=clock.sleep, log=lambda _: None)
        self.assertFalse(any(not call[3] for call in client.calls))

    def test_old_instance_cannot_pass_even_with_matching_health_sha(self):
        old = dict(POD, image=IMAGE)
        client = FakeClient(polls=[old])
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_deploy(client)
        self.assertFalse(any(not call[3] for call in client.calls))

    def test_missing_timestamps_require_observed_restart(self):
        before = dict(POD)
        before.pop("start_instance_ts")
        current = dict(before, image=IMAGE)
        client = FakeClient(before=before, polls=[current])
        client.restart_result = {"status": "AVAILABLE"}
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_deploy(client)
        for state in ("SPAWNING", "DELETING"):
            with self.subTest(state=state):
                client = FakeClient(before=before, polls=[dict(current, status=state), current])
                client.restart_result = {"status": "AVAILABLE"}
                self.run_deploy(client)

    def test_restart_acknowledgement_cannot_validate_stale_available_without_timestamps(self):
        before = dict(POD)
        before.pop("start_instance_ts")
        current = dict(before, image=IMAGE)
        for polls in ([current], [dict(current, status="UNKNOWN"), current]):
            with self.subTest(polls=polls):
                # The fake health endpoint would return the expected SHA, and
                # restart says RESTARTING. Neither establishes a new instance.
                client = FakeClient(before=before, polls=polls)
                self.assertEqual(client.restart_result, {"status": "RESTARTING"})
                with self.assertRaisesRegex(DeploymentError, "timed out"):
                    self.run_deploy(client)
                self.assertFalse(any(not call[3] for call in client.calls))

    def test_waits_for_matching_health_after_instance_restart(self):
        client = FakeClient(health=[(503, b""), (200, b'{"status":"ok","build_sha":"old"}'),
                                    (200, json.dumps({"status": "ok", "build_sha": SHA}).encode())])
        self.run_deploy(client)
        self.assertEqual(sum(not call[3] for call in client.calls), 3)

    def test_wrong_health_or_error_status_never_succeeds(self):
        for body in (b"", b"<html>login</html>", b'{"status":"ok","build_sha":"old"}',
                     json.dumps({"status": "error", "build_sha": SHA}).encode()):
            with self.subTest(body=body), self.assertRaisesRegex(DeploymentError, "timed out"):
                self.run_deploy(FakeClient(health=[(200, body)]))
        self.assertFalse(matches_health(b"", "node-frontend", SHA))

    def test_update_failure_does_not_restart(self):
        client = FakeClient()
        client.put_status = 403
        with self.assertRaisesRegex(DeploymentError, "HTTP 403"):
            self.run_deploy(client)
        self.assertFalse(client.restarted)
        client = FakeClient()
        client.persist_image = False
        with self.assertRaisesRegex(DeploymentError, "persist"):
            self.run_deploy(client)
        self.assertFalse(client.restarted)

    def test_failed_or_competing_rollout_fails(self):
        for pod in (new_pod(status="ERROR"), new_pod(status="COMPLETE"), dict(POD)):
            with self.subTest(pod=pod), self.assertRaises(DeploymentError):
                self.run_deploy(FakeClient(polls=[pod]))

    def test_stale_terminal_status_after_restart_waits_for_new_instance(self):
        for state in ("ERROR", "COMPLETE"):
            with self.subTest(state=state):
                before = dict(POD, status=state)
                stale = dict(before, image=IMAGE)
                client = FakeClient(before=before, polls=[stale, new_pod(status="SPAWNING"), new_pod()])
                self.run_deploy(client)
                self.assertEqual(sum(not call[3] for call in client.calls), 1)

    def test_terminal_failure_after_observed_poll_transition_fails(self):
        before = dict(POD, status="ERROR")
        stale = dict(before, image=IMAGE)
        client = FakeClient(before=before, polls=[dict(stale, status="SPAWNING"), stale])
        with self.assertRaisesRegex(DeploymentError, "entered ERROR"):
            self.run_deploy(client)
        client = FakeClient(before=before, polls=[stale])
        with self.assertRaisesRegex(DeploymentError, "timed out"):
            self.run_deploy(client)

    def test_malformed_networking_fails_before_updating_or_restarting(self):
        for networking in ([{"url": "service.example"}],
                           {"default": {"protocol": "http", "url": ["service.example"]}},
                           {"default": {"protocol": "http", "url": "https://[bad"}}):
            with self.subTest(networking=networking):
                client = FakeClient(before=dict(POD, networking=networking))
                with self.assertRaises(DeploymentError):
                    self.run_deploy(client)
                self.assertEqual([call[0] for call in client.calls], ["GET"])
                self.assertFalse(client.restarted)

    def test_readiness_and_transient_control_plane_errors_are_retried(self):
        client = FakeClient(polls=[503, new_pod(status_container={"ready": False}), new_pod()])
        self.run_deploy(client)
        self.assertEqual(sum(not call[3] for call in client.calls), 1)
        with self.assertRaisesRegex(DeploymentError, "HTTP 401"):
            self.run_deploy(FakeClient(polls=[401]))

    def test_health_endpoint_discovery_and_override(self):
        pod = {"networking": {"api": {"protocol": "http", "url": "api.example"},
                              "db": {"protocol": "tcp", "url": "db.example"}}}
        self.assertEqual(health_url(pod, "python"), "https://api.example/health")
        self.assertEqual(health_url({}, "python", "https://example/custom/health"),
                         "https://example/custom/health")
        pod["networking"]["web"] = {"protocol": "http", "url": "web.example"}
        with self.assertRaisesRegex(DeploymentError, "health-url"):
            health_url(pod, "python")
        for url in ("file:///tmp/token", "https://user:pass@example/health", "https://example/?token=secret"):
            with self.subTest(url=url), self.assertRaises(DeploymentError):
                health_url({}, "python", url)


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/destination")
            self.end_headers()
        elif self.path == "/truncated":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"8\r\nshort")
            self.close_connection = True
        elif self.path == "/bad-status":
            self.wfile.write(b"not an HTTP status\r\n\r\n")
            self.close_connection = True
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def recording_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HTTPBoundaryTests(unittest.TestCase):
    def test_malformed_http_is_reported_as_a_deployment_error(self):
        with recording_server() as (_, base):
            client = HTTPClient("deployment-secret", "health-secret")
            for path in ("/truncated", "/bad-status"):
                for api in (True, False):
                    with (self.subTest(path=path, api=api),
                          self.assertRaisesRegex(DeploymentError, "HTTP connection failed")):
                        client.request("GET", base + path, api=api)

    def test_health_requests_never_receive_deployment_token(self):
        with recording_server() as (server, base):
            client = HTTPClient("deployment-secret", "health-secret")
            client.request("GET", base + "/api", api=True)
            client.request("GET", base + "/health")
            self.assertEqual(server.requests[0][1]["X-Tapis-Token"], "deployment-secret")
            self.assertNotIn("Authorization", server.requests[0][1])
            self.assertNotIn("X-Tapis-Token", server.requests[1][1])
            self.assertEqual(server.requests[1][1]["Authorization"], "Bearer health-secret")

    def test_api_and_health_redirects_are_not_followed(self):
        with recording_server() as (server, base):
            client = HTTPClient("deployment-secret", "health-secret")
            for api in (True, False):
                self.assertEqual(client.request("GET", base + "/redirect", api=api)[0], 302)
            self.assertEqual([item[0] for item in server.requests], ["/redirect", "/redirect"])


class PushFreshnessTests(unittest.TestCase):
    def test_skipped_run_summary_does_not_claim_verified_deployment(self):
        with tempfile.NamedTemporaryFile(mode="r+") as summary:
            with mock.patch.dict('os.environ', {'TAPIS_TOKEN': 'tapis-token', 'GITHUB_STEP_SUMMARY': summary.name}), \
                    mock.patch('sys.argv', ['deploy_tapis.py', '--pod', 'servicepod', '--image', IMAGE,
                                          '--runtime', 'python', '--build-sha', SHA]), \
                    mock.patch('scripts.deploy_tapis.deploy', return_value={'skipped': True}):
                self.assertEqual(main(), 0)
            body = summary.read()
            self.assertIn('Skipped superseded automatic push', body)
            self.assertNotIn('Verified', body)
            self.assertNotIn('tapis-token', body)

    def test_latest_push_deploys_stale_push_skips_and_manual_rollback_bypasses_api(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/release/candidate",
               "GITHUB_REPOSITORY": "example/service", "GH_TOKEN": "read-token"}
        for head, expected in ((SHA, True), ("b" * 40, False)):
            with self.subTest(head=head):
                opener = mock.MagicMock()
                response = opener.open.return_value.__enter__.return_value
                response.read.return_value = json.dumps({"object": {"sha": head}}).encode()
                self.assertEqual(current_push(env, SHA, opener=opener), expected)
                request = opener.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith('/git/ref/heads/release%2Fcandidate'))
                self.assertEqual(request.get_header('Authorization'), 'Bearer read-token')
                self.assertIsNone(request.get_header('X-tapis-token'))
        opener = mock.MagicMock()
        self.assertTrue(current_push({**env, "GITHUB_EVENT_NAME": "workflow_dispatch"}, SHA, opener=opener))
        opener.open.assert_not_called()

    def test_unverifiable_push_fails_closed_without_disclosing_token_or_body(self):
        env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
               "GITHUB_REPOSITORY": "example/service", "GH_TOKEN": "read-token"}
        for body in (b'invalid', b'{}', b'{"object":{"sha":"bad"}}'):
            with self.subTest(body=body):
                opener = mock.MagicMock()
                opener.open.return_value.__enter__.return_value.read.return_value = body
                with self.assertRaisesRegex(DeploymentError, "Could not verify current branch HEAD") as error:
                    current_push(env, SHA, opener=opener)
                self.assertNotIn('read-token', str(error.exception))
        with self.assertRaisesRegex(DeploymentError, 'GH_TOKEN'):
            current_push({**env, "GH_TOKEN": ""}, SHA)


if __name__ == "__main__":
    unittest.main()
