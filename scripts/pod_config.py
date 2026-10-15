"""Validate the opt-in pod catalog independently of service build configuration."""

import argparse
import copy
import json
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

from .deploy_tapis import DeploymentError, checked_url
from .tapis_images import image_repository
from .validate_config import ConfigError, StrictValidator, UniqueKeyLoader, _check_json_values

ROOT = Path(__file__).resolve().parents[1]
USERNAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.@-]{0,127}\Z")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SECRET_ENV_LIMIT = 32 * 1024
DEFAULT_RESOURCES = {"cpu_request": 250, "cpu_limit": 2000, "mem_request": 256,
                     "mem_limit": 3072, "gpus": 0}


def required_admins(raw):
    """Central policy is two distinct Tapis accounts, never supplied by a pod file."""
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigError("TAPIS_REQUIRED_ADMINS must be a JSON array of two distinct Tapis usernames") from None
    if (not isinstance(names, list) or len(names) != 2
            or any(not isinstance(name, str) or not USERNAME.fullmatch(name) for name in names)
            or names[0] == names[1]):
        raise ConfigError("TAPIS_REQUIRED_ADMINS must contain exactly two distinct canonical Tapis usernames")
    return names


def merge_secret_environment(config, raw):
    """Merge optional secret JSON into a validated catalog without overriding it.

    Values are literal strings, including meaningful whitespace. Keep them out
    of validation messages, logs and workflow outputs. Provisioning compares the
    merged environment on reruns; changing a secret never silently updates a pod.
    """
    config = copy.deepcopy(config)
    if raw is None or raw == "":
        if config.get("secret-environment-keys"):
            raise ConfigError("POD_ENV_JSON is missing required environment values")
        return config
    if not isinstance(raw, str):
        raise ConfigError("POD_ENV_JSON must be a JSON object of string environment values")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError:
        raise ConfigError("POD_ENV_JSON must contain valid UTF-8 text") from None
    if size > SECRET_ENV_LIMIT:
        raise ConfigError("POD_ENV_JSON exceeds 32 KiB")

    def unique_object(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ConfigError("POD_ENV_JSON must not contain duplicate keys")
            values[key] = value
        return values

    try:
        values = json.loads(raw, object_pairs_hook=unique_object)
    except ConfigError:
        raise
    except (ValueError, RecursionError):
        raise ConfigError("POD_ENV_JSON must be a valid JSON object of string environment values") from None
    if not isinstance(values, dict):
        raise ConfigError("POD_ENV_JSON must be a JSON object of string environment values")
    for key, value in values.items():
        if not ENVIRONMENT_NAME.fullmatch(key):
            raise ConfigError("POD_ENV_JSON contains an invalid environment variable name")
        if not isinstance(value, str) or len(value) > 16384:
            raise ConfigError("POD_ENV_JSON values must be strings of at most 16384 characters")
        # Tapis accepts multiline strings. NUL and other non-whitespace controls
        # cannot be useful portable environment values; never trim credentials.
        if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159
               for char in value):
            raise ConfigError("POD_ENV_JSON values contain unsupported control characters")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ConfigError("POD_ENV_JSON values must contain valid UTF-8 text") from None
    if "PORT" in values:
        raise ConfigError("Configure PORT in the reviewed catalog, not POD_ENV_JSON")
    environment = config["environment-variables"]
    if values.keys() & environment.keys():
        raise ConfigError("POD_ENV_JSON must not override environment variables in the reviewed catalog")
    if any(key not in values or values[key] == "" for key in config.get("secret-environment-keys", [])):
        raise ConfigError("POD_ENV_JSON is missing required nonempty environment values")
    if values and len(environment) + len(values) > 100:
        raise ConfigError("Combined pod environment must contain at most 100 variables")
    environment.update(values)
    return config


def _safe_text(value):
    if isinstance(value, str):
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value):
            raise ConfigError("Catalog strings must not contain control characters or newlines")
    elif isinstance(value, dict):
        for key, item in value.items():
            _safe_text(key)
            _safe_text(item)
    elif isinstance(value, list):
        for item in value:
            _safe_text(item)


def validate_config(config):
    config = copy.deepcopy(config)
    _check_json_values(config)
    _safe_text(config)
    schema = json.loads((ROOT / "schema/pod-provisioning-v1.schema.json").read_text())
    errors = list(StrictValidator(schema).iter_errors(config))
    if errors:
        # jsonschema messages can include whole objects (and environment values).
        # Report only the schema path, never the rejected value.
        field = ".".join(str(part) for part in errors[0].absolute_schema_path)
        raise ConfigError(f"Invalid pod catalog configuration ({field})")
    try:
        image_repository(config["image"])
        if "health-url" in config:
            checked_url(config["health-url"])
    except DeploymentError as error:
        raise ConfigError(str(error)) from None
    tag_sha = re.search(r":sha-([0-9a-f]{40})-[0-9]+-[0-9]+$", config["image"])
    if tag_sha and tag_sha[1] != config["build-sha"]:
        raise ConfigError("build-sha must match the unique image tag")
    config.setdefault("volumes", [])
    seen_ids, seen_paths = set(), []
    for volume in config["volumes"]:
        path = volume["mount-path"]
        parsed = PurePosixPath(path)
        if (not path.startswith("/") or path.startswith("//") or str(parsed) != path
                or ".." in parsed.parts or path == "/"
                or any(parsed == root or root in parsed.parents or parsed in root.parents
                       for root in map(PurePosixPath, ("/app", "/proc", "/sys", "/dev", "/etc", "/tmp/nginx",
                                                       "/usr", "/bin", "/sbin", "/lib", "/lib64")))):
            raise ConfigError("Volume mounts require a normalized absolute data path outside application/system directories")
        if volume["volume-id"] in seen_ids:
            raise ConfigError("Each volume-id may occur only once per pod catalog entry")
        if any(parsed == other or parsed in other.parents or other in parsed.parents for other in seen_paths):
            raise ConfigError("Volume mount paths must not duplicate or overlap")
        seen_ids.add(volume["volume-id"])
        seen_paths.append(parsed)
        volume.setdefault("size-limit", 1024)
        volume.setdefault("read-only", False)
    resources = {**DEFAULT_RESOURCES, **config.get("resources", {})}
    if any(resources[name + "_request"] > resources[name + "_limit"] for name in ("cpu", "mem")):
        raise ConfigError("Resource requests must not exceed their limits")
    config["resources"] = resources
    config.setdefault("environment-variables", {})
    port = config["environment-variables"].setdefault("PORT", "5000")
    if not re.fullmatch(r"[1-9][0-9]{3,4}", port) or not 1024 <= int(port) <= 65535:
        raise ConfigError("environment-variables.PORT must be a string integer from 1024 to 65535")
    config["port"] = int(port)
    if set(config.get("secret-environment-keys", [])) & config["environment-variables"].keys():
        raise ConfigError("secret-environment-keys must not include PORT or public environment variables")
    config.setdefault("time-to-stop-default", -1)
    config.setdefault("rollout-timeout", 300)
    config.setdefault("health-url", "")
    return config


def load_config(path, *, catalog_root=None):
    path = Path(path)
    if catalog_root is not None:
        root = Path(catalog_root).resolve()
        resolved = path.resolve()
        # One reviewed file per pod; no aliases, subdirectories or symlink escapes.
        if (resolved.parent != root or path.is_symlink() or Path(catalog_root).is_symlink()
                or path.suffix != ".yaml"):
            raise ConfigError("Pod configuration must be a direct .yaml file in the trusted catalog")
    try:
        if path.stat().st_size > 256 * 1024:
            raise ConfigError("Pod configuration exceeds 256 KiB")
        config = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError):
        raise ConfigError("Could not read valid pod catalog YAML") from None
    except ConfigError:
        # Duplicate-key exceptions can contain attacker-controlled mapping keys.
        raise ConfigError("Invalid pod catalog YAML structure") from None
    config = validate_config(config)
    if catalog_root is not None and path.stem != config["pod-name"]:
        raise ConfigError("Catalog filename must exactly match pod-name")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--catalog-root")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, catalog_root=args.catalog_root)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                for key in ("pod-name", "runtime-type", "rollout-timeout"):
                    output.write(f"{key.replace('-', '_')}={config[key]}\n")
    except (ConfigError, OSError, RecursionError):
        print("Invalid pod catalog configuration; check the schema and trusted catalog path.", file=sys.stderr)
        return 1
    print(f"Validated pod catalog entry: {config['pod-name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
