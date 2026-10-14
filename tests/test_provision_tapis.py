"""Exercise create-only provisioning, least-privilege ACLs, and resumable failures."""

import copy
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from scripts.deploy_tapis import APIError, DeploymentError, HTTPConnectionError
from scripts import provision_tapis
from scripts.provision_tapis import payloads, permissions, provision


BASE = "https://tapis.example"
POD_URL = BASE + "/v3/pods/servicepod"
VOLUME_URL = BASE + "/v3/pods/volumes/servicedata"
SHA = "a" * 40
ADMINS = ["institute.one", "institute.two"]
OWNER = "institute/platform"
CONFIG = {
    "schema-version": 1, "pod-name": "servicepod", "runtime-type": "python",
    "image": "ghcr.io/institute/servicepod:sha-" + SHA, "build-sha": SHA,
    "developers": ["developer.one", "developer.two"],
    "volumes": [{"volume-id": "servicedata", "mount-path": "/data", "size-limit": 1024,
                 "read-only": False}],
    "environment-variables": {"PUBLIC_OPTION": "example", "PORT": "5000"}, "port": 5000,
    "resources": {"cpu_request": 250, "cpu_limit": 2000, "mem_request": 256, "mem_limit": 3072},
    "time-to-stop-default": -1, "rollout-timeout": 30, "health-url": "",
}


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class Client:
    def __init__(self, runtime="python"):
        self.calls = []
        self.resources = {}
        self.acls = {}
        self.runtime = runtime
        self.failures = {}
        self.lost_create = set()
        self.reject_permission = None
        self.ignore_permission = None
        self.mount_identity = "automation"
        self.start_status = "AVAILABLE"
        self.health_sha = SHA
        self.poll_count = 0

    @staticmethod
    def response(result):
        return 200, json.dumps({"status": "success", "result": result}).encode()

    def request(self, method, url, *, payload=None, api=False, timeout=10):
        self.calls.append((method, url, copy.deepcopy(payload), api, timeout))
        failure = self.failures.get((method, url))
        if failure:
            if isinstance(failure, Exception):
                raise failure
            return failure, b""
        if not api:
            body = (self.health_sha.encode() if self.runtime == "node-frontend"
                    else json.dumps({"status": "ok", "build_sha": self.health_sha}).encode())
            return 200, body
        if url.endswith("/permissions"):
            resource_url = url.removesuffix("/permissions")
            if method == "POST":
                if (resource_url, payload["user"]) == self.reject_permission:
                    return 403, b""
                if (resource_url, payload["user"]) != self.ignore_permission:
                    self.acls[resource_url][payload["user"]] = payload["level"]
            return self.response({"permissions": [f"{user}:{level}" for user, level in self.acls[resource_url].items()]})
        if url.endswith("/start"):
            self.resources[POD_URL].update(status_requested="ON", status=self.start_status,
                                           start_instance_ts="new-instance", networking_live=True)
            return self.response(self.resources[POD_URL])
        if method == "POST":
            value = copy.deepcopy(payload)
            if url.endswith("/volumes"):
                resource_url = url + "/" + payload["volume_id"]
                value["status"] = "AVAILABLE"
            else:
                resource_url = url + "/" + payload["pod_id"]
                value.update(status="STOPPED", start_instance_ts=None, networking_live=False,
                             status_container={"ready": True})
                value["networking"]["default"]["url"] = "service.pods.example"
                for mount in value["volume_mounts"].values():
                    mount["mounted_by"] = self.mount_identity
                    mount["sub_path"] = ""
            self.resources[resource_url] = value
            self.acls[resource_url] = {"automation": "ADMIN"}
            if resource_url in self.lost_create:
                raise HTTPConnectionError("lost response")
            return self.response(value)
        if method == "GET" and url in self.resources:
            self.poll_count += 1
            return self.response(self.resources[url])
        return 404, b""

    def seed(self, config=CONFIG):
        pod, volumes = payloads(config, OWNER)
        self.request("POST", BASE + "/v3/pods/volumes", payload=volumes[0], api=True)
        self.request("POST", BASE + "/v3/pods", payload=pod, api=True)
        self.calls.clear()

    def mutations(self):
        return [call for call in self.calls if call[0] != "GET" or call[1].endswith("/start")]


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.approval = mock.patch("scripts.provision_tapis.ensure_image_allowed")
        self.ensure_image = self.approval.start()
        self.addCleanup(self.approval.stop)

    def run_provision(self, client=None, config=None, *, apply=True, **kwargs):
        client = client or Client()
        config = copy.deepcopy(config or CONFIG)
        clock = Clock()
        return provision(client, config, ADMINS, base_url=BASE, tenant="institute", owner=OWNER,
                         apply=apply, clock=clock.now, sleep=clock.sleep, log=lambda _: None, **kwargs)

    def test_plan_is_read_only_and_validates_image_before_resource_reads(self):
        client = Client()
        self.ensure_image.side_effect = lambda *args, **kwargs: self.assertEqual(client.calls, [])
        result = self.run_provision(client, apply=False)
        self.assertEqual(client.mutations(), [])
        self.assertTrue(result["create_pod"])
        self.assertEqual(result["create_volumes"], ["servicedata"])
        self.assertFalse(result["apply"])

    def test_plan_describes_resources_and_permissions_without_secret_values(self):
        config = copy.deepcopy(CONFIG)
        config["environment-variables"]["PRIVATE_OPTION"] = "private-environment-value"
        client = Client()
        client.seed(config)
        logs = []
        clock = Clock()
        result = provision(client, config, ADMINS, base_url=BASE, tenant="institute", owner=OWNER,
                           apply=False, clock=clock.now, sleep=clock.sleep, log=logs.append)
        output = "\n".join(logs)
        for expected in ("resume pod servicepod", CONFIG["image"], "runtime: python", "PORT: 5000",
                         "lifetime: unlimited", "CPU request 250m / limit 2000m",
                         "memory request 256 MiB / limit 3072 MiB", "Volume servicedata: existing",
                         "mount /data", "1024 MB (informational)", "read-only: false",
                         "2 required ADMIN accounts", "2 developer USER accounts"):
            self.assertIn(expected, output)
        for secret in ("PRIVATE_OPTION", "private-environment-value", *ADMINS):
            self.assertNotIn(secret, output)
        self.assertEqual(client.mutations(), [])
        self.assertEqual(result["create_volumes"], [])

    def test_creates_all_runtimes_stopped_grants_user_then_starts_and_checks_sha(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            with self.subTest(runtime=runtime):
                config = dict(CONFIG, **{"runtime-type": runtime})
                client = Client(runtime)
                result = self.run_provision(client, config)
                posts = [call for call in client.calls if call[0] == "POST"]
                self.assertEqual(posts[0][1], BASE + "/v3/pods/volumes")
                create = next(call for call in posts if call[1] == BASE + "/v3/pods")
                self.assertEqual(create[2]["status_requested"], "OFF")
                self.assertEqual(create[2]["networking"], {"default": {"protocol": "http", "port": 5000}})
                self.assertEqual(create[2]["time_to_stop_default"], -1)
                self.assertNotIn("mounted_by", create[2]["volume_mounts"]["/data"])
                for url in (VOLUME_URL, POD_URL):
                    self.assertEqual(client.acls[url], {"automation": "ADMIN", ADMINS[0]: "ADMIN",
                                                       ADMINS[1]: "ADMIN", "developer.one": "USER",
                                                       "developer.two": "USER"})
                start_index = next(i for i, call in enumerate(client.calls) if call[1].endswith("/start"))
                grants = [i for i, call in enumerate(client.calls) if call[0] == "POST" and call[1].endswith("/permissions")]
                self.assertTrue(all(index < start_index for index in grants))
                suffix = "/healthcheck" if runtime == "node-frontend" else "/health"
                self.assertEqual(result["health_url"], "https://service.pods.example" + suffix)
                self.assertEqual(result["build_sha"], SHA)
                self.assertTrue(all(call[3] for call in client.calls if call[1].startswith(BASE)))
                self.assertTrue(all(not call[3] for call in client.calls if not call[1].startswith(BASE)))

    def test_missing_image_approval_never_writes_resources(self):
        client = Client()
        self.ensure_image.side_effect = DeploymentError("Image approval required")
        with self.assertRaisesRegex(DeploymentError, "approval"):
            self.run_provision(client)
        self.assertEqual(client.calls, [])

    def test_image_change_during_health_check_is_not_reported_as_success(self):
        client = Client()
        request = client.request

        def concurrent_update(method, url, **kwargs):
            response = request(method, url, **kwargs)
            if not kwargs.get("api"):
                client.resources[POD_URL]["image"] = "ghcr.io/institute/servicepod:another"
            return response

        client.request = concurrent_update
        with self.assertRaisesRegex(DeploymentError, "planned image"):
            self.run_provision(client)

    def test_pending_lifecycle_change_during_health_is_not_reported_as_success(self):
        for requested in ("OFF", "RESTART"):
            with self.subTest(requested=requested):
                client = Client()
                request = client.request

                def concurrent_change(method, url, **kwargs):
                    response = request(method, url, **kwargs)
                    if not kwargs.get("api"):
                        client.resources[POD_URL]["status_requested"] = requested
                    return response

                client.request = concurrent_change
                with self.assertRaisesRegex(DeploymentError, "deadline expired"):
                    self.run_provision(client)
                self.assertEqual(sum(not call[3] for call in client.calls), 1)

    def test_does_not_treat_authorization_failure_as_missing_resource(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                client = Client()
                client.failures[("GET", VOLUME_URL)] = status
                with self.assertRaises(APIError):
                    self.run_provision(client)
                self.assertEqual(client.mutations(), [])

    def test_all_resources_preflight_before_any_write(self):
        client = Client()
        config = copy.deepcopy(CONFIG)
        config["volumes"].append({"volume-id": "otherdata", "mount-path": "/more", "size-limit": 100,
                                  "read-only": True})
        client.resources[BASE + "/v3/pods/volumes/otherdata"] = {"description": "belongs to someone else"}
        with self.assertRaisesRegex(DeploymentError, "Existing volume"):
            self.run_provision(client, config)
        self.assertEqual(client.mutations(), [])

    def test_refuses_existing_unowned_or_changed_resources(self):
        for resource, field, value in ((POD_URL, "description", "unmanaged"),
                                       (POD_URL, "image", "ghcr.io/institute/servicepod:other"),
                                       (POD_URL, "environment_variables", {"OTHER": "secret-value"}),
                                       (POD_URL, "command", ["other"]),
                                       (VOLUME_URL, "size_limit", 2048)):
            with self.subTest(resource=resource, field=field):
                client = Client()
                client.seed()
                client.resources[resource][field] = value
                with self.assertRaises(DeploymentError) as error:
                    self.run_provision(client)
                self.assertNotIn("secret-value", str(error.exception))
                self.assertEqual(client.mutations(), [])

    def test_acl_failure_leaves_pod_off_and_volumes_retained_then_rerun_resumes(self):
        client = Client()
        client.reject_permission = (POD_URL, ADMINS[1])
        with self.assertRaises(APIError):
            self.run_provision(client)
        self.assertIn(VOLUME_URL, client.resources)
        self.assertEqual(client.resources[POD_URL]["status_requested"], "OFF")
        self.assertFalse(any(call[1].endswith("/start") for call in client.calls))
        client.reject_permission = None
        client.calls.clear()
        self.run_provision(client)
        self.assertFalse(any(call[0] == "POST" and not call[1].endswith("/permissions") for call in client.calls))
        self.assertEqual(sum(call[1].endswith("/start") for call in client.calls), 1)

    def test_failed_acl_readback_never_starts_pod(self):
        client = Client()
        client.ignore_permission = (POD_URL, ADMINS[1])
        with self.assertRaisesRegex(DeploymentError, "not persisted"):
            self.run_provision(client)
        self.assertEqual(client.resources[POD_URL]["status_requested"], "OFF")

    def test_repeated_apply_healthy_pod_performs_no_writes_or_restart(self):
        client = Client()
        self.run_provision(client)
        client.calls.clear()
        self.run_provision(client)
        self.assertEqual(client.mutations(), [])

    def test_lost_create_response_recovers_exact_id_without_duplicate_post(self):
        for url in (VOLUME_URL, POD_URL):
            with self.subTest(url=url):
                client = Client()
                client.lost_create = {url}
                self.run_provision(client)
                collection = url.rsplit("/", 1)[0]
                self.assertEqual(sum(call[0] == "POST" and call[1] == collection for call in client.calls), 1)

    def test_unconfirmed_create_failure_is_not_retried(self):
        client = Client()
        client.failures[("POST", BASE + "/v3/pods/volumes")] = HTTPConnectionError("lost response")
        with self.assertRaises(HTTPConnectionError):
            self.run_provision(client)
        self.assertEqual(len(client.mutations()), 1)
        self.assertEqual(client.resources, {})

    def test_start_failure_is_not_retried_and_resources_are_retained(self):
        client = Client()
        client.failures[("GET", POD_URL + "/start")] = 503
        with self.assertRaises(APIError):
            self.run_provision(client)
        self.assertEqual(sum(call[1].endswith("/start") for call in client.calls), 1)
        self.assertIn(VOLUME_URL, client.resources)
        self.assertIn(POD_URL, client.resources)

    def test_missing_volume_of_existing_pod_is_not_recreated(self):
        client = Client()
        client.seed()
        del client.resources[VOLUME_URL]
        with self.assertRaisesRegex(DeploymentError, "automatic storage recreation"):
            self.run_provision(client)
        self.assertEqual(client.mutations(), [])

    def test_admin_overlap_wins_and_unrelated_acl_is_retained(self):
        client = Client()
        config = copy.deepcopy(CONFIG)
        config["developers"].append(ADMINS[0])
        client.seed(config)
        client.acls[POD_URL]["auditor"] = "READ"
        self.run_provision(client, config)
        self.assertEqual(client.acls[POD_URL][ADMINS[0]], "ADMIN")
        self.assertEqual(client.acls[POD_URL]["auditor"], "READ")

    def test_existing_developer_admin_is_not_silently_demoted_or_called_user(self):
        client = Client()
        client.seed()
        client.acls[POD_URL]["developer.one"] = "ADMIN"
        with self.assertRaisesRegex(DeploymentError, "already has administrator"):
            self.run_provision(client)
        self.assertEqual(client.mutations(), [])

    def test_mount_identity_requires_both_pod_and_volume_permission(self):
        for identity in (None, "someoneelse"):
            with self.subTest(identity=identity):
                client = Client()
                client.mount_identity = identity
                with self.assertRaisesRegex(DeploymentError, "mount identity"):
                    self.run_provision(client)
                self.assertFalse(any(call[1].endswith("/start") for call in client.calls))

    def test_matching_health_sha_and_readiness_are_required_before_deadline(self):
        for failure in ("sha", "ready", "routing"):
            with self.subTest(failure=failure):
                client = Client()
                client.seed()
                if failure == "sha":
                    client.health_sha = "b" * 40
                elif failure == "ready":
                    client.resources[POD_URL]["status_container"]["ready"] = False
                else:
                    client.resources[POD_URL].update(status_requested="ON", status="AVAILABLE", networking_live=False)
                with self.assertRaisesRegex(DeploymentError, "deadline expired"):
                    self.run_provision(client)
                self.assertTrue(all(0 < call[4] <= 10 for call in client.calls))
                self.assertIn(VOLUME_URL, client.resources)

    def test_volume_readiness_is_required_before_pod_creation(self):
        client = Client()
        client.seed()
        del client.resources[POD_URL]
        client.resources[VOLUME_URL]["status"] = "CREATING"
        with self.assertRaisesRegex(DeploymentError, "deadline expired"):
            self.run_provision(client)
        self.assertNotIn(POD_URL, client.resources)

    def test_frontend_without_volumes_works(self):
        config = dict(CONFIG, **{"runtime-type": "node-frontend", "volumes": []})
        client = Client("node-frontend")
        self.run_provision(client, config)
        self.assertEqual(client.resources[POD_URL]["volume_mounts"], {})
        self.assertNotIn(VOLUME_URL, client.resources)

    def test_explicit_port_controls_environment_and_networking(self):
        config = copy.deepcopy(CONFIG)
        config["environment-variables"]["PORT"] = "8000"
        config["port"] = 8000
        client = Client()
        self.run_provision(client, config)
        pod = client.resources[POD_URL]
        self.assertEqual(pod["environment_variables"]["PORT"], "8000")
        self.assertEqual(pod["networking"]["default"]["port"], 8000)

    def test_unsafe_owner_or_nonmatching_image_is_rejected_before_api(self):
        for image in ("ghcr.io/institute/different:tag",):
            client = Client()
            with self.assertRaisesRegex(DeploymentError, "match pod-name"):
                self.run_provision(client, dict(CONFIG, image=image))
            self.assertEqual(client.calls, [])
        for owner, base in (("org/repo/other", BASE), (OWNER, "http://tapis.example")):
            with self.subTest(owner=owner, base=base), self.assertRaises(DeploymentError):
                provision(Client(), CONFIG, ADMINS, base_url=base, owner=owner, tenant="institute")

    def test_unsupported_duplicate_permissions_fail_closed(self):
        for values in (["one:ADMIN", "one:USER"], ["one:OWNER"], ["invalid"]):
            with self.subTest(values=values), self.assertRaises(DeploymentError):
                permissions({"permissions": values})

    def test_cli_uses_only_provisioning_credentials(self):
        env = {"TAPIS_PROVISION_TOKEN": "private-provision-token", "HEALTHCHECK_TOKEN": "private-health-token",
               "TAPIS_REQUIRED_ADMINS": json.dumps(ADMINS), "TAPIS_TENANT_ID": "other",
               "GITHUB_REPOSITORY": OWNER}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.argv", ["provision_tapis", "--config", "pod.yaml"]), \
                mock.patch.object(provision_tapis, "load_config", return_value=CONFIG), \
                mock.patch.object(provision_tapis, "HTTPClient") as factory, \
                mock.patch.object(provision_tapis, "provision", return_value={}) as execute:
            self.assertEqual(provision_tapis.main(), 0)
        factory.assert_called_once_with("private-provision-token", "private-health-token")
        self.assertEqual(execute.call_args.kwargs["tenant"], "other")
        self.assertFalse(execute.call_args.kwargs["apply"])
        self.assertEqual(execute.call_args.args[2], ADMINS)

    def test_cli_does_not_accept_ordinary_deployment_token(self):
        with mock.patch.dict(os.environ, {"TAPIS_TOKEN": "wrong-token"}, clear=True), \
                mock.patch("sys.argv", ["provision_tapis", "--config", "pod.yaml"]), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                provision_tapis.main()
        self.assertIn("TAPIS_PROVISION_TOKEN is required", output.getvalue())
        self.assertNotIn("wrong-token", output.getvalue())

    def test_cli_missing_approval_notifies_apply_once_and_never_plan(self):
        env = {"TAPIS_PROVISION_TOKEN": "private-provision-token", "TAPIS_REQUIRED_ADMINS": json.dumps(ADMINS)}
        for apply in (False, True):
            with self.subTest(apply=apply):
                arguments = ["provision_tapis", "--config", "pod.yaml"] + (["--apply"] if apply else [])
                with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.argv", arguments), \
                        mock.patch.object(provision_tapis, "load_config", return_value=CONFIG), \
                        mock.patch.object(provision_tapis, "provision", side_effect=provision_tapis.MissingImageApproval(CONFIG["image"], "institute")), \
                        mock.patch.object(provision_tapis, "notify_missing_image", return_value=False) as notify, \
                        contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(provision_tapis.main(), 1)
                    self.assertEqual(notify.call_count, int(apply))
                self.assertIn("approval required", output.getvalue())
                self.assertNotIn("private-provision-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
