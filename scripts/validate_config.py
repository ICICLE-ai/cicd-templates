"""Validate versioned service YAML locally and emit safe CI step outputs."""

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker, validators

SCHEMA_PATHS = {
    version: Path(__file__).resolve().parents[1] / f"schema/icicle-service-v{version}.schema.json"
    for version in (1, 2, 3)
}
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
RUNTIME_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
DEFAULTS = {
    "build-apt-packages": [],
    "runtime-apt-packages": [],
    "rollout-timeout": 300,
    "include-dev-dependencies": True,
    "npm-install-mode": "auto",
}
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: type(value) is int,
    ),
)


class ConfigError(ValueError):
    """A config problem that should be reported without a Python traceback."""


@dataclass
class ValidationResult:
    config: dict
    warnings: list

    def github_outputs(self):
        """Only known, validated scalar data can become runner step outputs."""
        values = {
            name.replace("-", "_"): str(self.config[name])
            for name in (
                "schema-version", "service-name", "service-version", "project-name",
                "service-type", "runtime-type", "runtime-version",
                "rollout-timeout",
            )
        }
        values.update(
            # Preserve the deployment interface without adding the forbidden
            # pod-name source field to schema-v3 normalized configuration.
            pod_name=self.config["service-name"] if self.config["schema-version"] == 3
            else self.config["pod-name"],
            build_apt=" ".join(self.config["build-apt-packages"]),
            runtime_apt=" ".join(self.config["runtime-apt-packages"]),
            health_url=self.config.get("health-url", ""),
            include_dev_dependencies=str(self.config["include-dev-dependencies"]).lower(),
            npm_install_mode=self.config["npm-install-mode"],
        )
        return values


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        explicit_keys = set()
        for key_node, _ in node.value:
            # YAML merge keys may intentionally override inherited values.
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConfigError("Configuration mapping keys must be strings")
            if key in explicit_keys:
                raise ConfigError(f"Duplicate YAML key: {key}")
            explicit_keys.add(key)
        return super().construct_mapping(node, deep=deep)


# Keep dates in custom metadata JSON-compatible instead of constructing Python
# date objects. All other standard YAML types are retained for strict validation.
UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [(tag, expression) for tag, expression in rules
          if tag != "tag:yaml.org,2002:timestamp"]
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _check_json_values(value, active=None):
    """Reject recursive YAML aliases and YAML-only values before JSON output."""
    active = set() if active is None else active
    if isinstance(value, (dict, list)):
        if id(value) in active:
            raise ConfigError("Recursive YAML aliases are not supported")
        active.add(id(value))
        for item in value.values() if isinstance(value, dict) else value:
            _check_json_values(item, active)
        active.remove(id(value))
    elif isinstance(value, float) and not math.isfinite(value):
        raise ConfigError("Nonfinite numbers are not supported")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ConfigError("Configuration values must be ordinary YAML/JSON data")


def _check_output_text(name, value):
    if isinstance(value, str) and CONTROL_CHARACTERS.search(value):
        raise ConfigError(f"{name} must not contain newlines or control characters")
    if isinstance(value, list):
        for item in value:
            _check_output_text(name, item)


def _check_health_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ConfigError(f"health-url is invalid: {error}") from error
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value
            or any(character.isspace() for character in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise ConfigError("health-url must be a full HTTP(S) URL without credentials, query, or fragment")


def load_config(path):
    path = Path(path)
    warnings = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    loader = UniqueKeyLoader(source)
    try:
        node = loader.get_single_node()
        config = loader.construct_document(node) if node is not None else None
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML: {error}") from error
    finally:
        loader.dispose()
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a YAML mapping")
    _check_json_values(config)
    version = config.get("schema-version")
    if type(version) is not int or version not in SCHEMA_PATHS:
        raise ConfigError("schema-version must be an integer supported version: 1, 2, or 3")
    if version == 3 and "pod-name" in config:
        raise ConfigError("schema-version 3 derives pod-name from service-name; remove pod-name from the service YAML")

    # Legacy yq accepted unquoted decimal runtime versions. Preserve their YAML
    # spelling (22.10 must not become 22.1), then apply the string schema.
    if isinstance(config.get("runtime-version"), float):
        spelling = next((value.value for key, value in node.value
                         if key.value == "runtime-version" and isinstance(value, yaml.ScalarNode)), "")
        if RUNTIME_VERSION.fullmatch(spelling):
            config["runtime-version"] = spelling
            warnings.append("runtime-version was numeric; quote it as a MAJOR.MINOR string")

    schema = json.loads(SCHEMA_PATHS[version].read_text(encoding="utf-8"))
    known = schema["properties"]
    for name, value in config.items():
        if name in known:
            _check_output_text(name, value)
        else:
            warnings.append(f"Unknown schema-v{version} field {name!r}; retained as metadata and ignored by CI")
    validator = StrictValidator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda error: str(list(error.path)))
    if errors:
        error = errors[0]
        field = ".".join(str(part) for part in error.path) or "configuration"
        raise ConfigError(f"{field}: {error.message}")
    if version == 2 and config["service-name"] != config["pod-name"]:
        raise ConfigError("schema-version 2 requires service-name to exactly equal pod-name")
    if config["runtime-type"] == "python" and "npm-install-mode" in config:
        raise ConfigError("npm-install-mode applies only to Node runtimes")
    if "health-url" in config:
        _check_health_url(config["health-url"])
    for name, value in DEFAULTS.items():
        config.setdefault(name, list(value) if isinstance(value, list) else value)
    return ValidationResult(config, warnings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="icicle-service.yaml")
    parser.add_argument("--github-output", type=Path, help="Append validated GitHub Actions step outputs")
    parser.add_argument("--json", action="store_true", help="Print normalized configuration as JSON")
    args = parser.parse_args(argv)
    try:
        result = load_config(args.config)
        normalized = json.dumps(result.config, indent=2, allow_nan=False)
        payload = "".join(f"{name}={value}\n" for name, value in result.github_outputs().items())
        # Validate everything before opening the runner output file. Invalid
        # configuration never leaves partial outputs behind.
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(payload)
    except (ConfigError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(normalized if args.json else f"Validated {args.config} (schema {result.config['schema-version']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
