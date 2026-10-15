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
import re
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
TEMPLATE_VERSION = "2.1.0"
NODE_VERSION = "24.22.0"
PYTHON_VERSION = "3.13.16"
TEMPLATE_REVISION = "abcdef0123456789abcdef0123456789abcdef01"
INDEX = b"<!doctype html><title>Image smoke fixture</title>"


def docker(*arguments, capture=True, check=True, timeout=60):
    return subprocess.run(
        ["docker", *arguments], text=True, capture_output=capture,
        check=check, timeout=timeout,
    )


def npm_install_commands(runtime):
    """Execute the real Docker RUN bodies locally; no duplicate npm implementation."""
    source = (ROOT / runtime / "Dockerfile").read_text().replace("\\\n", " ")
    commands = re.findall(r"^RUN --mount=type=cache,target=/root/\.npm (.+)$", source, re.M)
    if len(commands) != (3 if runtime == "node-backend" else 2):
        raise AssertionError("npm install stages changed; update contract extraction")
    return commands


@unittest.skipUnless(shutil.which("node") and shutil.which("npm") and shutil.which("cc"),
                     "npm contract tests need local Node, npm, and a C compiler")
class NpmInstallContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="icicle-npm-contract-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        shutil.copytree(ROOT / "tests/fixtures/npm-contract", self.source)
        (self.source / "icicle-service.yaml").write_text("schema-version: 2\n")
        self.environment = dict(os.environ, NPM_CONFIG_CACHE=str(self.directory / "cache"),
                                NPM_CONFIG_AUDIT="false", NPM_CONFIG_FUND="false",
                                NPM_CONFIG_UPDATE_NOTIFIER="false", NPM_INSTALL_MODE="auto",
                                NPM_CONFIG_OFFLINE="true", CI="true")
        self.environment.pop("NODE_ENV", None)

    def command(self, command, context, check=True):
        # Docker's /tmp markers must be isolated for concurrent local test runs.
        command = command.replace("/tmp/icicle-npm-", str(self.directory / "npm-"))
        command = command.replace("find /app ", "find " + str(context) + " ")
        result = subprocess.run(["sh", "-eu", "-c", command], cwd=context,
                                env=self.environment, capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def install(self, runtime, *, mode="auto", transform=None):
        if transform:
            transform(self.source)
        self.environment["NPM_INSTALL_MODE"] = mode
        context = self.directory / (runtime + "-" + mode)
        context.mkdir()
        for name in ("package.json", "package-lock.json", ".npmrc", "npm-shrinkwrap.json", "binding.gyp"):
            if (self.source / name).exists():
                shutil.copyfile(self.source / name, context / name)
        commands = npm_install_commands(runtime)
        self.command(commands[0], context)
        deferred = (self.directory / "npm-full-source").exists()
        shutil.copytree(self.source, context, dirs_exist_ok=True)
        self.command(commands[1], context)
        return context, deferred, commands

    def test_root_hooks_workspaces_npmrc_and_native_dependency_scripts(self):
        for runtime in ("node-backend", "node-frontend"):
            with self.subTest(runtime=runtime):
                context, deferred, commands = self.install(runtime)
                self.assertTrue(deferred)
                self.assertEqual((context / "prepared.txt").read_text(), "source is available")
                self.command("npm run build", context)
                self.assertEqual((context / "dist/index.html").read_text(), "source is available")
                self.assertEqual((context / "production-mode.txt").read_text(), "development")
                if runtime == "node-backend":
                    # Demonstrate why prune cannot generically replace production ci:
                    # it leaves source/dependency artifacts from development hooks.
                    self.command("npm prune --omit=dev", context)
                    self.assertEqual((context / "production-mode.txt").read_text(), "development")
                    self.command(commands[2], context)
                    self.assertEqual((context / "production-mode.txt").read_text(), "production")
                    self.assertFalse((context / ".npmrc").exists())
                    self.command("node -e 'if(require(\"runtime-dependency\")() !== \"dependency lifecycle ran\") process.exit(1)'", context)

    def test_root_prepare_without_workspaces_uses_full_source(self):
        def root_only(path):
            value = json.loads((path / "package.json").read_text())
            value.pop("workspaces")
            value.pop("dependencies")
            (path / "package.json").write_text(json.dumps(value))
            self.command("npm install --package-lock-only --ignore-scripts", path)
        context, deferred, _ = self.install("node-backend", transform=root_only)
        self.assertTrue(deferred)
        self.assertEqual((context / "prepared.txt").read_text(), "source is available")

    def test_workspace_without_root_hooks_uses_full_source(self):
        def workspace_only(path):
            value = json.loads((path / "package.json").read_text())
            value["scripts"] = {"build": "node hooks.js build"}
            (path / "package.json").write_text(json.dumps(value))
        context, deferred, _ = self.install("node-frontend", transform=workspace_only)
        self.assertTrue(deferred)
        self.command("npm run build", context)

    def test_plain_manifests_fast_path_and_explicit_full_mode(self):
        def plain(path):
            shutil.rmtree(path)
            shutil.copytree(ROOT / "tests/fixtures/node-backend", path)
            (path / "icicle-service.yaml").write_text("schema-version: 2\n")
            (path / ".npmrc").write_text("legacy-peer-deps=true\n")
        context, deferred, _ = self.install("node-backend", transform=plain)
        self.assertFalse(deferred)
        self.assertTrue((context / ".npmrc").exists())
        _context, deferred, _ = self.install("node-backend", mode="full")
        self.assertTrue(deferred)

    def test_mismatched_lockfile_is_rejected_instead_of_rewritten(self):
        package = self.source / "package.json"
        value = json.loads(package.read_text())
        value["dependencies"]["missing-dependency"] = "1.0.0"
        package.write_text(json.dumps(value))
        lock = (self.source / "package-lock.json").read_text()
        commands = npm_install_commands("node-backend")
        self.command(commands[0], self.source)
        result = self.command(commands[1], self.source, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.source / "package-lock.json").read_text(), lock)

    def test_auth_config_rejected_without_printing_credential(self):
        directives = (
            '//registry.example.test/:_authToken',
            '"//registry.example.test/:_authToken"',
            '"//registry.example.test/path;parameter/:_authToken"',
            '"//registry.example.test/path\\nsegment/:_authToken"',
            "'//registry.example.test/:_authToken'",
            '"//registry.example.test/:username"',
            "'//registry.example.test/:_password'",
            '"//registry.example.test/:certfile"',
            '"//registry.example.test/:keyfile"',
            '"cert"', '"key"',
        )
        for runtime in ("node-backend", "node-frontend"):
            for directive in directives:
                with self.subTest(runtime=runtime, directive=directive):
                    (self.source / ".npmrc").write_text(directive + "=fixture-do-not-print\n")
                    result = self.command(npm_install_commands(runtime)[0], self.source, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("fixture-do-not-print", result.stdout + result.stderr)
            with self.subTest(runtime=runtime, directive="credential URL"):
                (self.source / ".npmrc").write_text('"registry"="https://fixture-do-not-print:fixture-password@registry.example.test/"\n')
                result = self.command(npm_install_commands(runtime)[0], self.source, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("fixture-do-not-print", result.stdout + result.stderr)
                self.assertNotIn("fixture-password", result.stdout + result.stderr)

    def test_quoted_nonsecret_options_and_source_paths_are_normalized(self):
        shutil.rmtree(self.source)
        shutil.copytree(ROOT / "tests/fixtures/node-backend", self.source)
        for runtime in ("node-backend", "node-frontend"):
            for directive, deferred in (('"legacy-peer-deps"=true', False),
                                        ('"cafile"=./public-ca-bundle', True),
                                        ("'script-shell'=./custom-shell", True)):
                with self.subTest(runtime=runtime, directive=directive):
                    (self.source / ".npmrc").write_text(directive + "\n")
                    marker = self.directory / "npm-full-source"
                    marker.unlink(missing_ok=True)
                    self.command(npm_install_commands(runtime)[0], self.source)
                    self.assertEqual(marker.exists(), deferred)



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
        cls.contexts = {}
        cls.build_arguments = {}
        for runtime in ("python", "node-backend", "node-frontend"):
            context = Path(cls.temporary.name) / runtime
            shutil.copytree(ROOT / "tests" / "fixtures" / runtime, context)
            # Simulate the reusable workflow's downloads into a service repo.
            for name in ("Dockerfile", ".dockerignore"):
                shutil.copyfile(ROOT / runtime / name, context / name)
            (context / "icicle-service.yaml").write_text(
                "schema-version: 3\nservice-name: smokeservice\n"
                f"service-version: {BUILD_VERSION}\nproject-name: smoke\n"
                f"service-type: ai\nruntime-type: {runtime}\n"
                f'runtime-version: "{PYTHON_VERSION if runtime == "python" else NODE_VERSION}"\n'
                "build-apt-packages: []\nruntime-apt-packages: []\n"
            )
            if runtime == "node-frontend":
                for name in ("nginx.conf", "nginx-api.conf.template"):
                    shutil.copyfile(ROOT / runtime / name, context / name)
                shutil.copyfile(ROOT / runtime / "entrypoint.sh", context / "nginx-entrypoint.sh")
            cls.contexts[runtime] = context
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
                arguments = (
                    "build", "--progress=plain", "--tag", tag,
                    "--build-arg", f"PYTHON_VERSION={PYTHON_VERSION}",
                    "--build-arg", f"NODE_VERSION={NODE_VERSION}",
                    "--build-arg", f"BUILD_SHA={BUILD_SHA}",
                    "--build-arg", f"BUILD_VERSION={BUILD_VERSION}",
                    "--build-arg", f"TEMPLATE_VERSION={TEMPLATE_VERSION}",
                    "--build-arg", f"TEMPLATE_REVISION={TEMPLATE_REVISION}",
                    *extra, str(context),
                )
                docker(*arguments, capture=False, timeout=1800)
                cls.build_arguments[name] = arguments
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

    def test_source_edits_reuse_dependency_install_layers(self):
        for runtime, source, instruction in (
            ("python", "src/icicle_smoke/__main__.py", "uv sync --locked --no-install-project"),
            ("node-backend", "server.js", "NPM_INSTALL_MODE"),
            ("node-frontend", "build.js", "NPM_INSTALL_MODE"),
        ):
            with self.subTest(runtime=runtime):
                path = self.contexts[runtime] / source
                original = path.read_text()
                path.write_text(original + ("\n# cache regression edit\n" if runtime == "python"
                                           else "\n// cache regression edit\n"))
                try:
                    result = docker(*self.build_arguments[runtime], timeout=1800)
                    output = result.stdout + result.stderr
                    # BuildKit's actual cache status, not Dockerfile text or elapsed time.
                    steps = re.findall(r"^(#\d+) \[build-stage .*?\] RUN (.*)$", output, re.M)
                    candidates = [number for number, command in steps if instruction in command]
                    self.assertTrue(candidates, output)
                    self.assertTrue(any(re.search(r"^" + re.escape(number) + r" CACHED$", output, re.M)
                                        for number in candidates), output)
                    # A cached npm/uv stage is meaningful only if COPY . changed.
                    copies = re.findall(r"^(#\d+) \[build-stage .*?\] COPY \. /app$", output, re.M)
                    self.assertTrue(copies, output)
                    self.assertFalse(any(re.search(r"^" + re.escape(number) + r" CACHED$", output, re.M)
                                         for number in copies), output)
                finally:
                    path.write_text(original)

    def test_lock_and_npm_config_edits_invalidate_dependency_layers(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            context = self.contexts[runtime]
            lock = context / ("uv.lock" if runtime == "python" else "package-lock.json")
            original = lock.read_text()
            if runtime == "python":
                lock.write_text(original + "\n# lock cache regression edit\n")
            else:
                value = json.loads(original)
                value["fixture-cache-key"] = "changed"
                lock.write_text(json.dumps(value))
            try:
                with self.subTest(runtime=runtime, input="lock"):
                    result = docker(*self.build_arguments[runtime], timeout=1800)
                    output = result.stdout + result.stderr
                    marker = "uv sync --locked --no-install-project" if runtime == "python" else "NPM_INSTALL_MODE"
                    steps = re.findall(r"^(#\d+) \[build-stage .*?\] RUN (.*)$", output, re.M)
                    candidates = [number for number, command in steps if marker in command]
                    self.assertTrue(candidates, output)
                    self.assertFalse(any(re.search(r"^" + re.escape(number) + r" CACHED$", output, re.M)
                                         for number in candidates), output)
            finally:
                lock.write_text(original)
            if runtime != "python":
                npmrc = context / ".npmrc"
                npmrc.write_text("legacy-peer-deps=true\n")
                try:
                    with self.subTest(runtime=runtime, input="npmrc"):
                        result = docker(*self.build_arguments[runtime], timeout=1800)
                        output = result.stdout + result.stderr
                        steps = re.findall(r"^(#\d+) \[build-stage .*?\] RUN (.*)$", output, re.M)
                        candidates = [number for number, command in steps if "NPM_INSTALL_MODE" in command]
                        self.assertTrue(candidates, output)
                        self.assertFalse(any(re.search(r"^" + re.escape(number) + r" CACHED$", output, re.M)
                                             for number in candidates), output)
                finally:
                    npmrc.unlink()

    def test_full_source_npm_hooks_workspaces_and_native_builds_in_images(self):
        for runtime in ("node-backend", "node-frontend"):
            for mode in ("auto", "full"):
                with self.subTest(runtime=runtime, mode=mode):
                    context = Path(self.temporary.name) / (runtime + "-contract-" + mode)
                    shutil.copytree(ROOT / "tests/fixtures/npm-contract", context)
                    for source in self.contexts[runtime].iterdir():
                        if source.name in ("Dockerfile", ".dockerignore", "icicle-service.yaml", "nginx.conf",
                                           "nginx-api.conf.template", "nginx-entrypoint.sh"):
                            shutil.copyfile(source, context / source.name)
                    tag = self.prefix + ":" + context.name
                    self.addCleanup(docker, "image", "rm", "-f", tag, check=False)
                    version = "22.11" if runtime == "node-backend" and mode == "full" else NODE_VERSION
                    docker("build", "--progress=plain", "--tag", tag,
                           "--build-arg", f"NODE_VERSION={version}",
                           "--build-arg", "BUILD_APT_PACKAGES=build-essential",
                           "--build-arg", "NPM_INSTALL_MODE=" + mode, str(context),
                           capture=False, timeout=1800)
                    if runtime == "node-backend":
                        actual = docker("run", "--rm", "--entrypoint", "node", tag, "--version").stdout.strip()
                        if version == "22.11":
                            self.assertRegex(actual, r"^v22\.11\.[0-9]+$")
                        else:
                            self.assertEqual(actual, "v" + version)
                        command = (
                            'const fs=require("fs"); '
                            'if(fs.readFileSync("prepared.txt","utf8")!=="source is available")process.exit(1); '
                            'if(fs.readFileSync("production-mode.txt","utf8")!=="production")process.exit(1); '
                            'if(fs.existsSync(".npmrc"))process.exit(1); '
                            'if(require("runtime-dependency")()!=="dependency lifecycle ran")process.exit(1)'
                        )
                        docker("run", "--rm", "--entrypoint", "node", tag, "-e", command)
                    else:
                        value = docker("run", "--rm", "--entrypoint", "cat", tag, "/app/site/index.html").stdout
                        self.assertEqual(value, "source is available")

    def test_template_provenance_labels(self):
        for runtime, tag in self.images.items():
            with self.subTest(runtime=runtime):
                labels = json.loads(docker("image", "inspect", "--format", "{{json .Config.Labels}}", tag).stdout)
                self.assertEqual(labels["io.icicle.template.version"], TEMPLATE_VERSION)
                self.assertEqual(labels["io.icicle.template.revision"], TEMPLATE_REVISION)

    def test_python_installed_package_version_and_production_dependencies(self):
        for runtime, includes_dev in (("python", False), ("python-legacy-dev", True)):
            with self.subTest(runtime=runtime), self.container(runtime) as (name, port):
                value = self.assert_backend_health(port)
                # Fixture .python-version is 3.14; explicit build arg must win.
                self.assertEqual(value["python"], [3, 13])
                self.assertEqual(docker("exec", name, "python", "--version").stdout.strip(), "Python " + PYTHON_VERSION)
                self.assertIn("/site-packages/icicle_smoke/", value["module_path"])
                self.assertEqual(value["dev_dependency"], includes_dev)
                docker("exec", name, "python", "-c",
                       "import ssl; c=ssl.create_default_context(); "
                       "assert c.cert_store_stats()['x509_ca'] > 0; "
                       "assert ssl.get_default_verify_paths().cafile")

    def test_node_backend(self):
        with self.container("node-backend") as (name, port):
            self.assert_backend_health(port)
            self.assertEqual(docker("exec", name, "node", "--version").stdout.strip(), "v" + NODE_VERSION)
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
