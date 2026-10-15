"""Validate the operator catalog without executing application repository code."""

from pathlib import Path
import json
import tempfile
import unittest

import yaml

from scripts.pod_config import (ConfigError, load_config, main, merge_secret_environment,
                                required_admins, validate_config)

CONFIG = {"schema-version": 1, "pod-name": "myservice", "runtime-type": "python",
          "image": "ghcr.io/example/myservice:sha-" + "a" * 40 + "-123-1",
          "build-sha": "a" * 40, "developers": ["developerone"]}


class PodConfigTests(unittest.TestCase):
    def test_defaults_and_port_override_for_all_runtimes(self):
        for runtime in ("python", "node-backend", "node-frontend"):
            for port in (None, "8000", "65535", "1024"):
                with self.subTest(runtime=runtime, port=port):
                    source = {**CONFIG, "runtime-type": runtime}
                    if port:
                        source["environment-variables"] = {"PORT": port}
                    config = validate_config(source)
                    self.assertEqual(config["port"], int(port or "5000"))
                    self.assertEqual(config["environment-variables"]["PORT"], port or "5000")
                    self.assertEqual(config["time-to-stop-default"], -1)
                    self.assertEqual(config["volumes"], [])
                    self.assertEqual(config["resources"]["cpu_request"], 250)
        self.assertNotIn("environment-variables", CONFIG)

    def test_invalid_ports_and_types(self):
        for value in (5000, True, "05000", "80", "65536", "-1", "5000\n", "5000;command", "", None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config({**CONFIG, "environment-variables": {"PORT": value}})

    def test_admins_are_two_distinct_individual_accounts(self):
        self.assertEqual(required_admins('["adminone", "admintwo"]'), ["adminone", "admintwo"])
        self.assertEqual(required_admins('["_service", "admin-two"]'), ["_service", "admin-two"])
        for value in ("", "null", "{}", '["one"]', '["one", "one"]', '["one", "two", "three"]',
                      '["one", "tenant.*"]', '["one", "two:ADMIN"]', '["one", 2]', '["one", "two\\n"]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                required_admins(value)

    def test_secret_environment_merges_literal_strings_without_mutating_catalog(self):
        source = validate_config({**CONFIG, "environment-variables": {"PUBLIC_OPTION": "example"}})
        values = {"DATABASE_URL": "postgresql://user:password@database/service",
                  "KEY_DATA": "  first\r\nsecond\tline\n", "LITERAL": "${VALUE}; $(command) \\",
                  "EMPTY_VALUE": "", "UNICODE_VALUE": "café"}
        merged = merge_secret_environment(source, json.dumps(values))
        self.assertEqual(merged["environment-variables"], {**source["environment-variables"], **values})
        self.assertEqual(source["environment-variables"], {"PUBLIC_OPTION": "example", "PORT": "5000"})
        for raw in (None, "", "{}"):
            with self.subTest(raw=raw):
                result = merge_secret_environment(source, raw)
                self.assertEqual(result, source)
                self.assertIsNot(result, source)

    def test_secret_environment_rejects_invalid_json_and_values_without_echoing_them(self):
        secret = "credential-value-not-for-logs"
        invalid = [secret, " ", "[]", "null", "true", "1", json.dumps(secret),
                   '{"PASSWORD":"' + secret + '","PASSWORD":"other"}',
                   json.dumps({"PASSWORD": {"nested": secret}}), json.dumps({"PASSWORD": [secret]}),
                   json.dumps({"PASSWORD": 123}), json.dumps({"PASSWORD": True}),
                   json.dumps({"PASSWORD": None}), '{"PASSWORD":NaN}',
                   json.dumps({"PASSWORD": secret + "\x00"}), json.dumps({"PASSWORD": secret + "\x1b"}),
                   json.dumps({"PASSWORD": "\ud800"}), "\ud800",
                   '{"PASSWORD":' + '[' * 1500 + '"' + secret + '"' + ']' * 1500 + '}',
                   json.dumps({"PASSWORD": secret + "x" * 16384}),
                   json.dumps({"ONE": "x" * 16000, "TWO": "x" * 16000, "THREE": secret * 40}),
                   json.dumps({"PASSWORD": "é" * 16384}, ensure_ascii=False)]
        invalid.extend(json.dumps({name: secret}) for name in
                       ("", "1PASSWORD", "BAD-NAME", "BAD NAME", "PASSWORD\n", "PASSWORD\x00", "PÄSSWORD"))
        for raw in invalid:
            with self.subTest(case=invalid.index(raw)), self.assertRaises(ConfigError) as error:
                merge_secret_environment(validate_config(CONFIG), raw)
            self.assertNotIn(secret, str(error.exception))
        with self.assertRaises(ConfigError):
            merge_secret_environment(validate_config(CONFIG), {"PASSWORD": secret})

    def test_secret_environment_respects_limits_and_public_settings(self):
        config = validate_config({**CONFIG, "environment-variables": {"PUBLIC_OPTION": "example"}})
        for values in ({"PORT": "5000"}, {"PORT": "8000"}, {"PUBLIC_OPTION": "same-or-different"}):
            with self.subTest(keys=list(values)), self.assertRaises(ConfigError):
                merge_secret_environment(config, json.dumps(values))
        boundary = merge_secret_environment(config, json.dumps({"PASSWORD": "x" * 16384}))
        self.assertEqual(len(boundary["environment-variables"]["PASSWORD"]), 16384)
        maximum = {"KEY" + str(index): "value" for index in range(99)}
        merged = merge_secret_environment(validate_config(CONFIG), json.dumps(maximum))
        self.assertEqual(len(merged["environment-variables"]), 100)
        with self.assertRaises(ConfigError):
            merge_secret_environment(config, json.dumps(maximum))

    def test_required_secret_environment_keys_are_reviewed_and_must_have_values(self):
        config = validate_config({**CONFIG, "secret-environment-keys": ["DATABASE_URL", "SIGNING_KEY"]})
        for raw in (None, "", "{}", '{"DATABASE_URL":"value"}',
                    '{"DATABASE_URL":"value","SIGNING_KEY":""}'):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                merge_secret_environment(config, raw)
        values = {"DATABASE_URL": "value", "SIGNING_KEY": " \n ", "OPTIONAL_KEY": "extra"}
        merged = merge_secret_environment(config, json.dumps(values))
        self.assertEqual(merged["environment-variables"], {"PORT": "5000", **values})
        self.assertEqual(merged["secret-environment-keys"], ["DATABASE_URL", "SIGNING_KEY"])
        for changes in ({"secret-environment-keys": ["PORT"]},
                        {"secret-environment-keys": ["PUBLIC_OPTION"],
                         "environment-variables": {"PUBLIC_OPTION": "public"}},
                        {"secret-environment-keys": ["BAD-NAME"]},
                        {"secret-environment-keys": ["ONE", "ONE"]},
                        {"secret-environment-keys": [1]}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                validate_config({**CONFIG, **changes})

    def test_invalid_manifest_fails_without_echoing_environment_values(self):
        secret = "credential-value-not-for-logs"
        for changes in ({"schema-version": True}, {"schema-version": 2}, {"pod-name": "bad-name"},
                        {"pod-name": "images"}, {"pod-name": "admin"}, {"pod-name": "secrets"},
                        {"developers": []}, {"developers": ["one", "one"]},
                        {"developers": ["tenant.*"]}, {"developers": ["two:ADMIN"]},
                        {"environment-variables": {"SECRET": {"bad": secret}}},
                        {"resources": {"cpu_request": 3000, "cpu_limit": 250}},
                        {"resources": {"mem_request": True}}, {"time-to-stop-default": 1},
                        {"rollout-timeout": 29}, {"rollout-timeout": 1801},
                        {"runtime-type": "custom"}, {"admins": ["somebody"]},
                        {"build-sha": "b" * 40}, {"health-url": "https://user:password@host/health"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError) as error:
                validate_config({**CONFIG, **changes})
            self.assertNotIn(secret, str(error.exception))

    def test_mount_contract_and_reserved_paths(self):
        valid = validate_config({**CONFIG, "volumes": [{"volume-id": "mydata", "mount-path": "/data"}]})
        self.assertEqual(valid["volumes"][0]["size-limit"], 1024)
        self.assertFalse(valid["volumes"][0]["read-only"])
        for path in ("relative", "/", "/app", "/app/site", "/proc/data", "/sys", "/dev", "/etc",
                     "/tmp/nginx", "/tmp", "/usr", "/bin", "/lib", "/usr/local/bin", "/sbin", "/lib64",
                     "/data/../app", "/data//nested", "//data", "/data/", "/data/."):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                validate_config({**CONFIG, "volumes": [{"volume-id": "mydata", "mount-path": path}]})
        for volumes in ([{"volume-id": "mydata", "mount-path": "/data"}, {"volume-id": "mydata", "mount-path": "/other"}],
                        [{"volume-id": "mydata", "mount-path": "/data"}, {"volume-id": "otherdata", "mount-path": "/data/nested"}]):
            with self.assertRaises(ConfigError):
                validate_config({**CONFIG, "volumes": volumes})

    def test_strict_yaml_and_catalog_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "provisioning"
            catalog.mkdir()
            path = catalog / "myservice.yaml"
            path.write_text(yaml.safe_dump(CONFIG))
            self.assertEqual(load_config(path, catalog_root=catalog)["pod-name"], "myservice")
            outside = root / "myservice.yaml"
            outside.write_text(path.read_text())
            link = catalog / "alias.yaml"
            link.symlink_to(outside)
            for invalid in (outside, link):
                with self.assertRaises(ConfigError):
                    load_config(invalid, catalog_root=catalog)
            path.rename(catalog / "otherpod.yaml")
            with self.assertRaises(ConfigError):
                load_config(catalog / "otherpod.yaml", catalog_root=catalog)
            for text in (yaml.safe_dump(CONFIG) + "pod-name: otherpod\n", "bad: [", "value: &v [*v]", "null"):
                path.write_text(text)
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_cli_only_emits_validated_scalars_after_full_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "myservice.yaml"
            output = root / "output"
            path.write_text(yaml.safe_dump(CONFIG))
            self.assertEqual(main(["--config", str(path), "--catalog-root", str(root),
                                   "--github-output", str(output)]), 0)
            before = output.read_text()
            self.assertEqual(before, "pod_name=myservice\nruntime_type=python\nrollout_timeout=300\n")
            path.write_text("developers: []")
            self.assertEqual(main(["--config", str(path), "--github-output", str(output)]), 1)
            self.assertEqual(output.read_text(), before)

    def test_example_is_valid(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(load_config(root / "provisioning-example.yaml")["port"], 5000)

    def test_reviewed_catalog_entries_validate_in_ci(self):
        catalog = Path(__file__).resolve().parents[1] / "provisioning"
        self.assertFalse(catalog.is_symlink())
        self.assertTrue(catalog.is_dir())
        for path in catalog.glob("*.yaml"):
            with self.subTest(pod=path.stem):
                load_config(path, catalog_root=catalog)


if __name__ == "__main__":
    unittest.main()
