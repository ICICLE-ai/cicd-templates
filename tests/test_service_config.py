"""Exercise the shared typed parser and its CLI before any CI outputs exist."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from scripts.validate_config import SCHEMA_PATHS, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate_config.py"
WORKFLOW = ROOT / ".github/workflows/deploy-service.yaml"
BASELINE = {
    "schema-version": 1,
    "service-name": "example-service",
    "service-version": "1.2.3",
    "service-type": "ai",
    "pod-name": "examplepod",
    "runtime-type": "node-frontend",
    "runtime-version": "22.11",
    "project-name": "example-project",
}


def workflow_script(name):
    """Read the actual literal run block without copying its implementation."""
    lines = WORKFLOW.read_text().splitlines()
    step = lines.index(f"      - name: {name}")
    end = next((index for index in range(step + 1, len(lines))
                if lines[index].startswith("      - ")), len(lines))
    run = lines.index("        run: |", step, end)
    body = lines[run + 1:end]
    if any(line and not line.startswith("          ") for line in body):
        raise AssertionError(f"{name} run block layout changed; update extraction")
    script = textwrap.dedent("\n".join(body))
    if "${{" in script:
        raise AssertionError("Pass config inputs via env, not shell source interpolation")
    return script


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="service-config-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "icicle-service.yaml"

    def write_config(self, changes=None):
        value = copy.deepcopy(BASELINE)
        value.update(changes or {})
        self.path.write_text(yaml.safe_dump(value))
        return value

    def write_v3_config(self, changes=None):
        value = copy.deepcopy(BASELINE)
        del value["pod-name"]
        value.update({"schema-version": 3, "service-name": "exampleservice"})
        value.update(changes or {})
        self.path.write_text(yaml.safe_dump(value))
        return value

    def assert_rejected(self, field, value):
        self.write_config({field: value})
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.path), *map(str, arguments)],
            capture_output=True, text=True, timeout=5, check=False,
        )

    def test_schemas_and_published_examples_pass(self):
        for version, schema_path in SCHEMA_PATHS.items():
            with self.subTest(version=version):
                Draft202012Validator.check_schema(json.loads(schema_path.read_text()))
        for name in ("icicle-service-example.yaml", "icicle-service-v3-example.yaml"):
            with self.subTest(example=name):
                result = load_config(ROOT / name)
                self.assertEqual(result.config["schema-version"], 3)
                self.assertEqual(result.config["service-name"], result.github_outputs()["pod_name"])
                self.assertNotIn("pod-name", result.config)
                self.assertNotIn("pod-name", yaml.safe_load((ROOT / name).read_text()))

    def test_v1_still_accepts_different_service_and_pod_names(self):
        self.write_config({"service-name": "existing-service", "pod-name": "existingpod"})
        result = load_config(self.path)
        self.assertEqual(result.github_outputs()["service_name"], "existing-service")
        self.assertEqual(result.github_outputs()["pod_name"], "existingpod")

    def test_v2_matching_names_work_for_all_runtimes(self):
        for runtime, runtime_version in (("python", "3.13"), ("node-backend", "22.11"),
                                         ("node-frontend", "22.11")):
            for name in ("abcd", "a123", "exampleservice", "a" * 64):
                with self.subTest(runtime=runtime, name=name):
                    self.write_config({"schema-version": 2, "service-name": name, "pod-name": name,
                                       "runtime-type": runtime, "runtime-version": runtime_version})
                    result = load_config(self.path)
                    self.assertEqual(result.github_outputs()["schema_version"], "2")
                    self.assertEqual(result.github_outputs()["service_name"], name)
                    self.assertEqual(result.github_outputs()["pod_name"], name)
                    self.assertEqual(result.config["rollout-timeout"], 300)
                    self.assertIs(result.config["include-dev-dependencies"], True)

    def test_v2_invalid_or_different_names_fail_before_outputs(self):
        output = self.directory / "github-output"
        for service, pod in (("serviceone", "servicetwo"), ("front-end", "frontend"),
                             ("frontend", "front-end"), ("abc", "abc"), ("1abc", "1abc"),
                             ("Frontend", "Frontend"), ("a" * 65, "a" * 65),
                             ("front\nend", "frontend")):
            with self.subTest(service_name=service, pod_name=pod):
                self.write_config({"schema-version": 2, "service-name": service, "pod-name": pod})
                result = self.cli("--github-output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertEqual(result.stdout, "")
        for field in ("service-name", "pod-name"):
            with self.subTest(missing=field):
                value = self.write_config({"schema-version": 2, "service-name": "frontend", "pod-name": "frontend"})
                del value[field]
                self.path.write_text(yaml.safe_dump(value))
                self.assertNotEqual(self.cli("--github-output", output).returncode, 0)
                self.assertFalse(output.exists())

    def test_v3_derives_pod_name_for_all_runtimes_and_boundaries(self):
        output = self.directory / "github-output"
        for runtime, runtime_version in (("python", "3.13"), ("node-backend", "22.11"),
                                         ("node-frontend", "22.11")):
            for name in ("abcd", "a123", "exampleservice", "a" * 64):
                with self.subTest(runtime=runtime, name=name):
                    source = self.write_v3_config({"service-name": name, "runtime-type": runtime,
                                                   "runtime-version": runtime_version})
                    output.unlink(missing_ok=True)
                    result = self.cli("--json", "--github-output", output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    normalized = json.loads(result.stdout)
                    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                    self.assertNotIn("pod-name", source)
                    self.assertNotIn("pod-name", normalized)
                    self.assertEqual(values["schema_version"], "3")
                    self.assertEqual(values["service_name"], name)
                    self.assertEqual(values["pod_name"], name)
                    self.assertEqual(values["runtime_type"], runtime)
                    self.assertEqual(values["rollout_timeout"], "300")
                    self.assertEqual(self.path.read_text(), yaml.safe_dump(source))
                    # --json remains a valid service document, not a different
                    # format containing the forbidden internal pod-name field.
                    Draft202012Validator(json.loads(SCHEMA_PATHS[3].read_text())).validate(normalized)
                    if runtime != "python":
                        self.path.write_text(result.stdout)
                        self.assertEqual(load_config(self.path).github_outputs(), values)

    def test_v3_rejects_invalid_service_names_before_outputs(self):
        output = self.directory / "github-output"
        for name in (None, True, 1234, [], {}, "", "a", "abc", "1abc", "a" * 65,
                     "Example", "examplePod", "front-end", "front_end", "front.end",
                     "front/end", "front:end", "front end", " front", "front ",
                     "front\tend", "front\nend", "front\n", "front\r", "front$end",
                     "éxample", "前端服务", "ｆｒｏｎｔ", "fron😀t"):
            with self.subTest(service_name=name):
                self.write_v3_config({"service-name": name})
                result = self.cli("--github-output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertFalse(output.exists())

    def test_v3_rejects_reserved_pod_names(self):
        for name in ("catalog", "snapshots", "volumes", "admin", "catalogs", "snapshot",
                     "volume", "image", "images", "template", "templates", "stack",
                     "stacks", "nodes", "metrics", "secrets"):
            with self.subTest(service_name=name):
                self.write_v3_config({"service-name": name})
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_v3_requires_service_name_and_rejects_any_explicit_pod_name(self):
        output = self.directory / "github-output"
        for pod in ("exampleservice", "anotherpod", "", None, True, 1234, [], {}):
            with self.subTest(pod_name=pod):
                self.write_v3_config({"pod-name": pod})
                result = self.cli("--json", "--github-output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("remove pod-name", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(output.exists())
        source = self.write_v3_config()
        del source["service-name"]
        self.path.write_text(yaml.safe_dump(source))
        result = self.cli("--github-output", output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("service-name", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(output.exists())

    def test_schema_version_selection_is_strict_and_explicit(self):
        for version in (None, True, False, "1", "2", "3", 1.0, 2.0, 3.0, 0, 4, 99, "../v1"):
            with self.subTest(version=version):
                self.write_config({"schema-version": version})
                with self.assertRaisesRegex(ConfigError, "schema-version.*1, 2, or 3"):
                    load_config(self.path)

    def test_cli_and_metadata_warnings_report_the_selected_schema(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                changes = {"schema-version": version, "service-name": "frontend",
                           "custom-metadata": {"owner": "team"}}
                if version == 3:
                    self.write_v3_config(changes)
                else:
                    self.write_config({**changes, "pod-name": "frontend"})
                result = self.cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"(schema {version})", result.stdout)
                self.assertIn(f"Unknown schema-v{version} field", result.stderr)
                self.assertEqual(load_config(self.path).config["custom-metadata"], {"owner": "team"})

    def test_valid_baselines_and_optional_defaults_for_all_runtimes(self):
        for runtime, version in (("python", "3.13"), ("node-backend", "22.11"),
                                 ("node-frontend", "22.11")):
            for service_type in ("ai", "ci", "domain"):
                with self.subTest(runtime=runtime, service_type=service_type):
                    self.write_config({"runtime-type": runtime, "runtime-version": version,
                                       "service-type": service_type})
                    result = load_config(self.path)
                    self.assertEqual(result.config["build-apt-packages"], [])
                    self.assertEqual(result.config["runtime-apt-packages"], [])
                    self.assertEqual(result.config["rollout-timeout"], 300)
                    self.assertIs(result.config["include-dev-dependencies"], True)
                    self.assertEqual(result.github_outputs()["runtime_version"], version)
                    self.assertEqual(result.github_outputs()["health_url"], "")
                    self.assertEqual(result.github_outputs()["include_dev_dependencies"], "true")

    def test_exact_runtime_patch_versions_are_preserved(self):
        for schema_version in (1, 2):
            for runtime, versions in (("python", ("3.13", "3.13.9")),
                                      ("node-backend", ("22.11", "22.11.0", "24.22.0")),
                                      ("node-frontend", ("22.11", "24.22.0"))):
                for version in versions:
                    with self.subTest(schema=schema_version, runtime=runtime, version=version):
                        self.write_config({"schema-version": schema_version, "service-name": "frontend",
                                           "pod-name": "frontend", "runtime-type": runtime,
                                           "runtime-version": version})
                        result = load_config(self.path)
                        self.assertEqual(result.config["runtime-version"], version)
                        self.assertEqual(result.github_outputs()["runtime_version"], version)
        for invalid in ("24.22.0.1", "24..0", "v24.22.0", "24.22.x", "24.22.0 ", "24.22.0\n"):
            with self.subTest(invalid=invalid):
                self.assert_rejected("runtime-version", invalid)

    def test_npm_install_mode_defaults_validation_and_outputs(self):
        for schema_version in (1, 2):
            for runtime in ("node-backend", "node-frontend"):
                for mode in ("auto", "full"):
                    self.write_config({"schema-version": schema_version, "service-name": "frontend",
                                       "pod-name": "frontend", "runtime-type": runtime,
                                       "npm-install-mode": mode})
                    self.assertEqual(load_config(self.path).github_outputs()["npm_install_mode"], mode)
        self.write_config()
        self.assertEqual(load_config(self.path).github_outputs()["npm_install_mode"], "auto")
        for invalid in ("manifests", "ignore-scripts", "", True, None):
            self.assert_rejected("npm-install-mode", invalid)
        self.write_config({"runtime-type": "python", "npm-install-mode": "full"})
        with self.assertRaisesRegex(ConfigError, "Node runtimes"):
            load_config(self.path)

    def test_required_keys_and_types_are_enforced(self):
        for field in BASELINE:
            with self.subTest(missing=field):
                value = self.write_config()
                del value[field]
                self.path.write_text(yaml.safe_dump(value))
                with self.assertRaises(ConfigError):
                    load_config(self.path)
            for invalid in (None, True, [], {}):
                with self.subTest(field=field, value=invalid):
                    self.assert_rejected(field, invalid)
        for invalid in ("1", 1.0, 4):
            with self.subTest(schema_version=invalid):
                self.assert_rejected("schema-version", invalid)

    def test_valid_pod_name_boundaries(self):
        for pod_name in ("abcd", "a123", "examplepod", "frontend123", "a" * 64, "null"):
            with self.subTest(pod_name=pod_name):
                self.write_config({"pod-name": pod_name})
                self.assertEqual(load_config(self.path).config["pod-name"], pod_name)

    def test_invalid_pod_names(self):
        for pod_name in (
            "", "a", "ab", "abc", "1abc", "1234", "-abc", "a" * 65,
            "ExamplePod", "examplePod", "FRONTEND", "front-end", "front_end",
            "front.end", "front/end", "front:end", "front end", " front", "front ",
            "front\tend", "front\nend", "front\n", "front\r", "front$end",
            "éxample", "fronténd", "前端服务", "ｆｒｏｎｔ", "fron😀t",
        ):
            with self.subTest(pod_name=pod_name):
                self.assert_rejected("pod-name", pod_name)

    def test_other_identity_and_runtime_constraints(self):
        for field, value in (
            ("service-name", "Bad_Name"), ("service-name", "valid\ninvalid?"),
            ("service-name", "orders-"),
            ("service-version", "1.2"), ("service-type", "unknown"),
            ("runtime-type", "node"), ("runtime-version", "22"),
            ("runtime-version", 22), ("project-name", ""), ("project-name", "   "),
        ):
            with self.subTest(field=field, value=value):
                self.assert_rejected(field, value)
        for name in ("a", "orders", "orders-api", "orders-api2"):
            with self.subTest(service_name=name):
                self.write_config({"service-name": name})
                self.assertEqual(load_config(self.path).config["service-name"], name)

    def test_legacy_numeric_runtime_version_preserves_yaml_precision_with_warning(self):
        for schema_version in (1, 2):
            for spelling in ("22.10", "3.13", "22.0"):
                with self.subTest(schema_version=schema_version, version=spelling):
                    self.write_config({"schema-version": schema_version,
                                       "service-name": "frontend", "pod-name": "frontend"})
                    source = self.path.read_text().replace("runtime-version: '22.11'",
                                                           f"runtime-version: {spelling}")
                    self.path.write_text(source)
                    result = load_config(self.path)
                    self.assertEqual(result.config["runtime-version"], spelling)
                    self.assertTrue(any("quote" in warning for warning in result.warnings))

    def test_duplicate_keys_and_invalid_yaml_are_rejected(self):
        self.write_config()
        source = self.path.read_text()
        for broken in (source + "pod-name: anotherpod\n", "[bad: yaml", "- a\n- b\n", "",
                       source + "metadata: {same: first, same: second}\n"):
            with self.subTest(source=broken):
                self.path.write_text(broken)
                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_safe_yaml_merge_overrides_are_supported(self):
        self.write_config()
        self.path.write_text("defaults: &defaults\n  project-name: old-project\n<<: *defaults\n"
                             + self.path.read_text())
        self.assertEqual(load_config(self.path).config["project-name"], "example-project")

    def test_known_strings_cannot_inject_runner_output_lines(self):
        for field, value in (("project-name", "project\npod_name=anotherpod"),
                             ("project-name", "project\x1b[31m"),
                             ("project-name", "project\x7f"),
                             ("build-apt-packages", ["curl\nruntime_apt=bad"])):
            with self.subTest(field=field, value=value):
                self.assert_rejected(field, value)

    def test_package_names_architectures_and_exact_versions(self):
        packages = ["gcc", "build-essential", "libstdc++6", "python3.13-dev",
                    "libc6:amd64", "libllvm14:arm64=1:14.0.6-12+deb12u1",
                    "nginx=1.26.3-3~deb13u1"]
        self.write_config({"build-apt-packages": packages, "runtime-apt-packages": packages})
        result = load_config(self.path)
        self.assertEqual(result.github_outputs()["build_apt"], " ".join(packages))
        for invalid in ("curl", None, [None], [True], [1], ["--allow-unauthenticated"],
                        ["curl git"], ["curl;id"], ["$(id)"], ["curl*"], ["curl/latest"],
                        ["CURL"], ["curl="], ["curl:bad/arch"], ["curl=1*"], ["a"]):
            with self.subTest(packages=invalid):
                self.assert_rejected("build-apt-packages", invalid)

    def test_health_url_and_rollout_fields(self):
        for url in ("https://frontend.example.test/healthcheck", "http://backend:3000/health",
                    "http://[::1]:8000/health"):
            with self.subTest(url=url):
                self.write_config({"health-url": url, "rollout-timeout": 30,
                                   "include-dev-dependencies": True})
                outputs = load_config(self.path).github_outputs()
                self.assertEqual(outputs["health_url"], url)
                self.assertEqual(outputs["rollout_timeout"], "30")
                self.assertEqual(outputs["include_dev_dependencies"], "true")
        for invalid in ("/health", "ftp://backend/health", "https://", "",
                        "https://user:secret@backend/health", "https://backend/health?q=1",
                        "https://backend/health#ready", "https://backend/health?",
                        "https://backend:0/health", "https://backend:65536/health",
                        "https://backend/a b", "https://backend/health\n"):
            with self.subTest(health_url=invalid):
                self.assert_rejected("health-url", invalid)
        for invalid in (0, 29, 1801, "300", 30.0, True):
            with self.subTest(timeout=invalid):
                self.assert_rejected("rollout-timeout", invalid)
        for invalid in ("true", 1, None):
            with self.subTest(include_dev=invalid):
                self.assert_rejected("include-dev-dependencies", invalid)
        self.write_config({"include-dev-dependencies": False})
        self.assertEqual(load_config(self.path).github_outputs()["include_dev_dependencies"], "false")

    def test_unknown_metadata_is_preserved_warned_and_not_emitted(self):
        metadata = {"notes": "one\ntwo", "owner": "team", "date": "2026-10-14"}
        self.write_config({"custom-metadata": metadata})
        result = load_config(self.path)
        self.assertEqual(result.config["custom-metadata"], metadata)
        self.assertTrue(any("custom-metadata" in warning for warning in result.warnings))
        self.assertNotIn("custom_metadata", result.github_outputs())

    def test_recursive_aliases_and_nonfinite_metadata_fail_cleanly(self):
        self.write_config()
        source = self.path.read_text()
        for suffix in ("metadata: &loop [*loop]\n", "metadata: .nan\n"):
            self.path.write_text(source + suffix)
            with self.assertRaises(ConfigError):
                load_config(self.path)

    def test_cli_emits_normalized_json_and_complete_outputs(self):
        self.write_config({"health-url": "https://frontend.example.test/healthcheck"})
        output = self.directory / "github-output"
        result = self.cli("--json", "--github-output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["runtime-version"], "22.11")
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values, load_config(self.path).github_outputs())

    def test_cli_invalid_input_leaves_no_partial_outputs_and_executes_no_payload(self):
        output = self.directory / "github-output"
        marker = self.directory / "executed"
        for field, value in (("pod-name", f"$(touch {marker})"),
                             ("build-apt-packages", [f"`touch {marker}`"]),
                             ("project-name", "project\npod_name=anotherpod")):
            with self.subTest(field=field):
                self.write_config({field: value})
                output.write_text("existing=value\n")
                result = self.cli("--github-output", output)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("ERROR:", result.stderr)
                self.assertEqual(output.read_text(), "existing=value\n")
                self.assertFalse(marker.exists())
        output.unlink()
        self.assertNotEqual(self.cli("--github-output", output).returncode, 0)
        self.assertFalse(output.exists())

    def test_buildtime_arguments_preserve_validated_package_values(self):
        script = workflow_script("Set up Buildtime Args")
        output = self.directory / "build-output"
        for runtime, argument in (("python", "PYTHON_VERSION"),
                                  ("node-backend", "NODE_VERSION"),
                                  ("node-frontend", "NODE_VERSION")):
            with self.subTest(runtime=runtime):
                self.write_config({"runtime-type": runtime, "build-apt-packages": ["gcc", "libc6:amd64"]})
                values = load_config(self.path).github_outputs()
                output.write_text("")
                environment = dict(
                    os.environ, RUNTIME=runtime, RUNTIME_VERSION=values["runtime_version"],
                    BUILD_APT_PACKAGES=values["build_apt"], RUNTIME_APT_PACKAGES=values["runtime_apt"],
                    BUILD_SHA="test-sha", GITHUB_OUTPUT=str(output),
                    BUILD_VERSION=values["service_version"],
                    INCLUDE_DEV_DEPENDENCIES=values["include_dev_dependencies"],
                    NPM_INSTALL_MODE=values["npm_install_mode"],
                )
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", script], env=environment,
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"{argument}=22.11\n", output.read_text())
                self.assertIn("BUILD_APT_PACKAGES=gcc libc6:amd64\n", output.read_text())
                self.assertIn("BUILD_VERSION=1.2.3\n", output.read_text())

    def test_cli_identity_reaches_image_and_deploy_arguments_for_all_schemas(self):
        steps = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["jobs"]["deploy"]["steps"]
        image = next(step for step in steps if step.get("name") == "Compute image references")
        deploy = next(step for step in steps if step.get("name") == "Deploy image and verify rollout")
        self.assertEqual(image["env"]["SERVICE_NAME"], "${{ steps.config.outputs.service_name }}")
        self.assertEqual(deploy["env"]["POD_NAME"], "${{ steps.config.outputs.pod_name }}")
        self.assertEqual(deploy["env"]["IMAGE"], "${{ steps.image.outputs.deploy_tag }}")

        # Run the actual workflow shell blocks, recording the deployment CLI
        # arguments in place of making a Tapis request.
        scripts = self.directory / "scripts"
        scripts.mkdir()
        (scripts / "deploy_tapis.py").write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['DEPLOY_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))\n"
        )
        bin_directory = self.directory / "bin"
        bin_directory.mkdir()
        (bin_directory / "python").symlink_to(sys.executable)
        config_output = self.directory / "config-output"
        image_output = self.directory / "image-output"
        arguments = self.directory / "deployment-arguments.json"
        sha = "a" * 40
        for version, service, pod in ((1, "existing-service", "existingpod"),
                                      (2, "exampleservice", "exampleservice"),
                                      (3, "exampleservice", "exampleservice")):
            for runtime in ("python", "node-backend", "node-frontend"):
                with self.subTest(schema=version, runtime=runtime):
                    changes = {"schema-version": version, "service-name": service,
                               "runtime-type": runtime,
                               "runtime-version": "3.13" if runtime == "python" else "22.11"}
                    if version == 3:
                        self.write_v3_config(changes)
                    else:
                        self.write_config({**changes, "pod-name": pod})
                    config_output.unlink(missing_ok=True)
                    result = self.cli("--github-output", config_output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = dict(line.split("=", 1) for line in config_output.read_text().splitlines())
                    image_output.write_text("")
                    environment = {
                        **os.environ, "REGISTRY": "ghcr.io", "OWNER": "Example-Org",
                        "SERVICE_NAME": values["service_name"], "SERVICE_VERSION": values["service_version"],
                        "BUILD_SHA": sha, "RUN_ID": "123", "RUN_ATTEMPT": "1",
                        "GITHUB_OUTPUT": str(image_output),
                    }
                    result = subprocess.run(["bash", "-eu", "-c", image["run"]], env=environment,
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    images = dict(line.split("=", 1) for line in image_output.read_text().splitlines())
                    expected_image = f"ghcr.io/example-org/{service}:sha-{sha}-123-1"
                    self.assertEqual(images["tag"], f"ghcr.io/example-org/{service}:1.2.3")
                    self.assertEqual(images["deploy_tag"], expected_image)
                    environment.update(
                        POD_NAME=values["pod_name"], RUNTIME=values["runtime_type"],
                        IMAGE=images["deploy_tag"], IMAGE_DIGEST="sha256:" + "b" * 64,
                        HEALTH_URL=values["health_url"], ROLLOUT_TIMEOUT=values["rollout_timeout"],
                        TEMPLATES_DIR=str(self.directory), DEPLOY_ARGUMENTS=str(arguments),
                        GITHUB_STEP_SUMMARY=str(self.directory / "summary"),
                        PATH=str(bin_directory) + os.pathsep + os.environ["PATH"],
                    )
                    result = subprocess.run(["bash", "-eu", "-c", deploy["run"]], env=environment,
                                            capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(arguments.read_text()), [
                        "--pod", pod, "--image", expected_image, "--runtime", runtime,
                        "--build-sha", sha, "--health-url", "", "--timeout", "300",
                    ])


if __name__ == "__main__":
    unittest.main()
