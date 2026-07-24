from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _string(data: dict[str, Any], name: str, default: str | None = None) -> str:
    value = data.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _positive_int(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _bounded_positive_int(
    data: dict[str, Any],
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    value = data.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > maximum:
        raise ConfigError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _boolean(data: dict[str, Any], name: str, default: bool) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _managed_runtime_path(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"runtime path must stay under the target .shea directory: {raw}")
    resolved_root = root.resolve()
    managed_root = (resolved_root / ".shea").resolve()
    if not managed_root.is_relative_to(resolved_root):
        raise ConfigError("target .shea directory escapes the target repository")
    resolved = (resolved_root / candidate).resolve()
    if resolved == managed_root or not resolved.is_relative_to(managed_root):
        raise ConfigError(f"runtime path must stay below the target .shea directory: {raw}")
    return resolved


def _relative_glob(raw: str) -> str:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"observation glob must stay under the target repository: {raw}")
    return raw


def validate_catalyst_sdk_base_endpoint(value: str) -> str:
    name = "catalyst_sdk_base_endpoint"
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"observation.{name} must be an HTTP(S) base URL")
    if value != value.strip() or any(character.isspace() for character in value):
        raise ConfigError(f"observation.{name} must be an HTTP(S) base URL")

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"observation.{name} must be an HTTP(S) base URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError(f"observation.{name} must be an HTTP(S) base URL") from exc

    normalized_path = parsed.path.rstrip("/")
    if normalized_path.endswith("/v1/traces"):
        base_path = normalized_path[: -len("/v1/traces")]
        return parsed._replace(path=base_path).geturl()
    if re.search(r"(?:^|/)v1/traces(?:/|$)", parsed.path):
        raise ConfigError(
            f"observation.{name} may end with /v1/traces but cannot contain content after it"
        )
    return value


def _catalyst_sdk_base_endpoint(data: dict[str, Any]) -> str:
    legacy_name = "desktop_otlp_base_endpoint"
    if legacy_name in data:
        raise ConfigError(
            "observation.desktop_otlp_base_endpoint is not supported; "
            "use observation.catalyst_sdk_base_endpoint"
        )
    return validate_catalyst_sdk_base_endpoint(
        _string(data, "catalyst_sdk_base_endpoint", "http://127.0.0.1:8799")
    )


def _commands(data: dict[str, Any], table_name: str) -> tuple[tuple[str, ...], ...]:
    raw_commands = data.get("commands", [])
    if not isinstance(raw_commands, list):
        raise ConfigError(f"{table_name}.commands must be a list")
    commands: list[tuple[str, ...]] = []
    for command in raw_commands:
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ConfigError(f"each {table_name} command must be a non-empty argv list")
        commands.append(tuple(command))
    return tuple(commands)


def _environment_names(data: dict[str, Any], table_name: str) -> tuple[str, ...]:
    raw_names = data.get("pass_env", [])
    if not isinstance(raw_names, list) or not all(
        isinstance(name, str) and _ENVIRONMENT_NAME.fullmatch(name) for name in raw_names
    ):
        raise ConfigError(f"{table_name}.pass_env must be a list of environment names")
    return tuple(sorted(set(raw_names)))


def _external_blocker_exit_codes(data: dict[str, Any]) -> frozenset[int]:
    raw_codes = data.get("external_blocker_exit_codes", [])
    if not isinstance(raw_codes, list) or not all(
        isinstance(code, int) and not isinstance(code, bool) and 1 <= code <= 255
        for code in raw_codes
    ):
        raise ConfigError(
            "experiments.external_blocker_exit_codes must contain integers from 1 to 255"
        )
    return frozenset(raw_codes)


def _trusted_producers(data: dict[str, Any]) -> tuple[str, ...]:
    raw = data.get("trusted_producers", [])
    if not isinstance(raw, list) or not all(
        isinstance(producer, str) and producer.strip() for producer in raw
    ):
        raise ConfigError("tracker.trusted_producers must be a list of GitHub logins")
    return tuple(sorted({producer.strip() for producer in raw}, key=str.casefold))


@dataclass(frozen=True)
class TrackerConfig:
    owner: str
    owner_type: str
    project_number: int
    status_field: str
    research_state: str
    ready_state: str
    done_state: str
    blocked_state: str
    trusted_producers: tuple[str, ...]


@dataclass(frozen=True)
class ObservationConfig:
    trace_globs: tuple[str, ...]
    desktop_report_globs: tuple[str, ...]
    catalyst_sdk_base_endpoint: str
    require_candidate_traces: bool


@dataclass(frozen=True)
class HaloConfig:
    root: Path
    repository: str
    base_branch: str
    tracker: TrackerConfig
    observation: ObservationConfig
    setup_commands: tuple[tuple[str, ...], ...]
    experiment_commands: tuple[tuple[str, ...], ...]
    external_blocker_exit_codes: frozenset[int]
    target_environment_variables: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    logs_dir: Path
    artifacts_dir: Path
    worktrees_dir: Path
    model: str
    investigator_max_turns: int
    halo_model: str

    @classmethod
    def load(cls, root: Path) -> HaloConfig:
        root = root.resolve()
        local_config = root / ".shea" / "halo.local.toml"
        uses_local_config = local_config.is_file()
        config_path = local_config if uses_local_config else root / ".shea" / "halo.toml"
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"missing target config: {config_path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid target config: {exc}") from exc

        if data.get("schema_version") != "shea-halo/v1":
            raise ConfigError("schema_version must be shea-halo/v1")

        target = _table(data, "target")
        tracker = _table(data, "tracker")
        observation = _table(data, "observation")
        if "catalog_url" in observation:
            raise ConfigError("observation.catalog_url is fixed by the Shea Halo protocol")
        setup = _table(data, "setup")
        experiments = _table(data, "experiments")
        verification = _table(data, "verification")
        runtime = _table(data, "runtime")
        models = _table(data, "models")
        target_environment_variables = _environment_names(experiments, "experiments")
        if target_environment_variables and not uses_local_config:
            raise ConfigError("experiments.pass_env is allowed only in ignored halo.local.toml")

        trace_globs = observation.get("trace_globs", [".shea/artifacts/halo/traces/*.jsonl"])
        if not isinstance(trace_globs, list) or not all(
            isinstance(item, str) and item for item in trace_globs
        ):
            raise ConfigError("observation.trace_globs must be a list of strings")
        desktop_report_globs = observation.get(
            "desktop_report_globs",
            [".shea/artifacts/halo/desktop/**/report.md"],
        )
        if not isinstance(desktop_report_globs, list) or not all(
            isinstance(item, str) and item for item in desktop_report_globs
        ):
            raise ConfigError("observation.desktop_report_globs must be a list of strings")

        owner_type = _string(tracker, "owner_type", "user")
        if owner_type not in {"user", "organization"}:
            raise ConfigError("tracker.owner_type must be user or organization")
        research_state = _string(tracker, "research_state", "Halo Research")
        ready_state = _string(tracker, "ready_state", "Todo")
        done_state = _string(tracker, "done_state", "Done")
        blocked_state = _string(tracker, "blocked_state", "Need Human Input")
        status_names = (research_state, ready_state, done_state, blocked_state)
        if len({name.casefold() for name in status_names}) != len(status_names):
            raise ConfigError("tracker research, ready, done, and blocked states must be distinct")

        return cls(
            root=root,
            repository=_string(target, "repository"),
            base_branch=_string(target, "base_branch", "main"),
            tracker=TrackerConfig(
                owner=_string(tracker, "owner"),
                owner_type=owner_type,
                project_number=_positive_int(tracker, "project_number"),
                status_field=_string(tracker, "status_field", "Status"),
                research_state=research_state,
                ready_state=ready_state,
                done_state=done_state,
                blocked_state=blocked_state,
                trusted_producers=_trusted_producers(tracker),
            ),
            observation=ObservationConfig(
                trace_globs=tuple(_relative_glob(item) for item in trace_globs),
                desktop_report_globs=tuple(_relative_glob(item) for item in desktop_report_globs),
                catalyst_sdk_base_endpoint=_catalyst_sdk_base_endpoint(observation),
                require_candidate_traces=_boolean(
                    observation,
                    "require_candidate_traces",
                    True,
                ),
            ),
            setup_commands=_commands(setup, "setup"),
            experiment_commands=_commands(experiments, "experiments"),
            external_blocker_exit_codes=_external_blocker_exit_codes(experiments),
            target_environment_variables=target_environment_variables,
            verification_commands=_commands(verification, "verification"),
            logs_dir=_managed_runtime_path(root, _string(runtime, "logs", ".shea/logs/halo")),
            artifacts_dir=_managed_runtime_path(
                root, _string(runtime, "artifacts", ".shea/artifacts/halo")
            ),
            worktrees_dir=_managed_runtime_path(
                root, _string(runtime, "worktrees", ".shea/worktrees/halo")
            ),
            model=_string(models, "investigator", os.getenv("OPENAI_MODEL", "gpt-5.6")),
            investigator_max_turns=_bounded_positive_int(
                models,
                "investigator_max_turns",
                default=30,
                maximum=100,
            ),
            halo_model=_string(models, "halo", os.getenv("HALO_MODEL", "gpt-5.6")),
        )
