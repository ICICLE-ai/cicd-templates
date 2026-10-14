"""Build and smoke-test all real images with minimal service repositories.

Run explicitly with: REQUIRE_DOCKER=1 python3 tests/test_images.py -v
Ordinary unittest discovery skips these network/container tests unless enabled.
Requires a running Docker daemon with BuildKit and network access for image,
Debian, Node, Python, and locked dependency downloads. No host files are mounted
into running containers. Every image/network/container created here is temporary.
"""

import contextlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SHA = "0123456789abcdef0123456789abcdef01234567"
BUILD_VERSION = "1.2.3"  # Deliberately differs from package metadata, 0.1.0.
TEMPLATE_VERSION = "2.0.0"
TEMPLATE_REVISION = "abcdef0123456789abcdef0123456789abcdef01"
INDEX = b"<!doctype html><title>Image smoke fixture</title>"


def docker(*arguments, capture=True, check=True, timeout=60):
    return subprocess.run(
        ["docker", *arguments], text=True, capture_output=capture,
        check=check, timeout=timeout,
    )


class ImageSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("REQUIRE_DOCKER") != "1":
            raise unittest.SkipTest("Image tests require explicit REQUIRE_DOCKER=1")
        if not shutil.which("docker"):
            raise RuntimeError("REQUIRE_DOCKER=1 but Docker is not installed")
        docker("info")
        cls.prefix = "icicle-smoke-" + uuid.uuid4().hex[:10]
        cls.temporary = tempfile.TemporaryDirectory(prefix=cls.prefix)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.images = {}
        for runtime in ("python", "node-backend", "node-frontend"):
            context = Path(cls.temporary.name) / runtime
            shutil.copytree(ROOT / "tests" / "fixtures" / runtime, context)
            # Simulate the reusable workflow's downloads into a service repo.
            for name in ("Dockerfile", ".dockerignore"):
                shutil.copyfile(ROOT / runtime / name, context / name)
            (context / "icicle-service.yaml").write_text(
                "schema-version: 2\nservice-name: smokeservice\n"
                f"service-version: {BUILD_VERSION}\nproject-name: smoke\n"
                f"service-type: ai\npod-name: smokeservice\nruntime-type: {runtime}\n"
                f'runtime-version: "{"3.13" if runtime == "python" else "22.11"}"\n'
                "build-apt-packages: []\nruntime-apt-packages: []\n"
            )
            if runtime == "node-frontend":
                for name in ("nginx.conf", "nginx-api.conf.template"):
                    shutil.copyfile(ROOT / runtime / name, context / name)
                shutil.copyfile(ROOT / runtime / "entrypoint.sh", context / "nginx-entrypoint.sh")
            variants = ((runtime, []),)
            if runtime == "python":
                variants = (
                    ("python", ["--build-arg", "INCLUDE_DEV_DEPENDENCIES=false"]),
                    ("python-legacy-dev", []),  # Omission preserves existing builds.
                )
            for name, extra in variants:
                tag = f"{cls.prefix}:{name}"
                # Register cleanup before building so partial failures are safe.
                cls.addClassCleanup(docker, "image", "rm", "-f", tag, check=False)
                docker(
                    "build", "--progress=plain", "--tag", tag,
                    "--build-arg", "PYTHON_VERSION=3.13",
                    "--build-arg", "NODE_VERSION=22.11",
                    "--build-arg", f"BUILD_SHA={BUILD_SHA}",
                    "--build-arg", f"BUILD_VERSION={BUILD_VERSION}",
                    "--build-arg", f"TEMPLATE_VERSION={TEMPLATE_VERSION}",
                    "--build-arg", f"TEMPLATE_REVISION={TEMPLATE_REVISION}",
                    *extra, str(context), capture=False, timeout=1800,
                )
                cls.images[name] = tag

    @contextlib.contextmanager
    def container(self, runtime, *options):
        name = f"{self.prefix}-{uuid.uuid4().hex[:8]}"
        docker("run", "--detach", "--name", name, "--publish", "127.0.0.1::8000", *options, self.images[runtime])
        try:
            port = int(docker("port", name, "8000/tcp").stdout.strip().rsplit(":", 1)[1])
            health = "/healthcheck" if runtime == "node-frontend" else "/health"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if self.request(port, health)[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() != "true":
                    self.fail("Container exited during startup:\n" + docker("logs", name).stdout)
                time.sleep(0.1)
            else:
                self.fail("Container did not become ready:\n" + docker("logs", name).stdout)
            self.assertEqual(docker("exec", name, "id", "-u").stdout.strip(), "999")
            command = docker("exec", name, "cat", "/proc/1/cmdline").stdout
            self.assertFalse(command.startswith("sh\0"), "Entrypoint did not exec the server")
            yield name, port
            docker("stop", "--time", "10", name, timeout=20)
            self.assertEqual(docker("inspect", "-f", "{{.State.ExitCode}}", name).stdout.strip(), "0")
        finally:
            docker("rm", "--force", name, check=False)

    @staticmethod
    def request(port, path, method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def assert_backend_health(self, port):
        status, _, body = self.request(port, "/health")
        value = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(value["uid"], 999)
        self.assertEqual(value["version"], BUILD_VERSION)
        self.assertEqual(value["build_sha"], BUILD_SHA)
        return value

    def test_template_provenance_labels(self):
        for runtime, tag in self.images.items():
            with self.subTest(runtime=runtime):
                labels = json.loads(docker("image", "inspect", "--format", "{{json .Config.Labels}}", tag).stdout)
                self.assertEqual(labels["io.icicle.template.version"], TEMPLATE_VERSION)
                self.assertEqual(labels["io.icicle.template.revision"], TEMPLATE_REVISION)

    def test_python_installed_package_version_and_production_dependencies(self):
        for runtime, includes_dev in (("python", False), ("python-legacy-dev", True)):
            with self.subTest(runtime=runtime), self.container(runtime) as (_name, port):
                value = self.assert_backend_health(port)
                # Fixture .python-version is 3.14; explicit build arg must win.
                self.assertEqual(value["python"], [3, 13])
                self.assertIn("/site-packages/icicle_smoke/", value["module_path"])
                self.assertEqual(value["dev_dependency"], includes_dev)

    def test_node_backend(self):
        with self.container("node-backend") as (_name, port):
            self.assert_backend_health(port)
            value = json.loads(self.request(port, "/api/tasks?limit=10")[2])
            self.assertEqual(value["path"], "/api/tasks?limit=10")

    def assert_frontend(self, port):
        status, headers, body = self.request(port, "/healthcheck")
        self.assertEqual((status, body), (200, BUILD_SHA.encode()))
        self.assertIn("no-store", headers["Cache-Control"])
        for path in ("/", "/index.html", "/example/details"):
            status, headers, body = self.request(port, path)
            self.assertEqual((status, body), (200, INDEX))
            self.assertIn("no-store", headers["Cache-Control"])
        status, headers, _ = self.request(port, "/assets/app.1234abcd.js")
        self.assertEqual(status, 200)
        self.assertIn("immutable", ",".join(headers.get_all("Cache-Control")))
        self.assertEqual(self.request(port, "/assets/missing.js")[0], 404)

    def test_frontend_without_backend(self):
        with self.container("node-frontend") as (name, port):
            self.assert_frontend(port)
            self.assertEqual(self.request(port, "/api/tasks")[2], INDEX)
            self.assertEqual(docker("exec", name, "stat", "-c", "%u", "/tmp/nginx/nginx.conf").stdout.strip(), "999")
            docker("exec", name, "rm", "/app/site/.build_sha")
            self.assertEqual(self.request(port, "/healthcheck")[2], b"")

    def test_same_frontend_image_with_backend_on_shared_network(self):
        network = self.prefix + "-network"
        docker("network", "create", network)
        try:
            with (
                self.container("node-backend", "--network", network, "--network-alias", "backend"),
                self.container("node-frontend", "--network", network, "--env", "BACKEND_URL=http://backend:8000") as (_name, port),
            ):
                self.assert_frontend(port)
                path, body = "/api/example/items?next=%2Fexample&tag=one&tag=two", '{"test":true}'
                status, _, response = self.request(port, path, "POST", body)
                value = json.loads(response)
                self.assertEqual(status, 200)
                self.assertEqual((value["path"], value["method"], value["body"]), (path, "POST", body))
        finally:
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    os.environ.setdefault("REQUIRE_DOCKER", "1")
    unittest.main()
