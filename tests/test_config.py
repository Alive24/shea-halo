from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_target_config

from shea_halo.config import ConfigError, HaloConfig


def test_loads_minimal_target_config(tmp_path: Path) -> None:
    write_target_config(tmp_path)

    config = HaloConfig.load(tmp_path)

    assert config.repository == "Alive24/FailureReport"
    assert config.tracker.research_state == "Halo Research"
    assert config.tracker.trusted_producers == ()
    assert config.worktrees_dir == tmp_path / ".shea/worktrees/halo"
    assert config.observation.catalyst_sdk_base_endpoint == "http://127.0.0.1:8799"
    assert config.observation.require_candidate_traces is True
    assert config.setup_commands == (("python", "-m", "pip", "--version"),)
    assert config.experiment_commands == (("python", "-m", "pytest"),)
    assert config.external_blocker_exit_codes == frozenset()
    assert config.target_environment_variables == ()
    assert config.verification_commands == (("python", "-m", "pytest"),)
    assert config.investigator_max_turns == 30


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:8799", "http://127.0.0.1:8799"),
        ("https://telemetry.example.com/collector", "https://telemetry.example.com/collector"),
        ("https://telemetry.example.com/collector/", "https://telemetry.example.com/collector/"),
        ("http://127.0.0.1:8799/v1/traces", "http://127.0.0.1:8799"),
        (
            "https://telemetry.example.com/collector/v1/traces/",
            "https://telemetry.example.com/collector",
        ),
    ],
)
def test_accepts_and_normalizes_catalyst_sdk_endpoint(
    tmp_path: Path,
    endpoint: str,
    expected: str,
) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            f'[observation]\ncatalyst_sdk_base_endpoint = "{endpoint}"',
        ),
        encoding="utf-8",
    )

    config = HaloConfig.load(tmp_path)

    assert config.observation.catalyst_sdk_base_endpoint == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "grpc://127.0.0.1:8799",
        "http:///missing-host",
        "http://user@example.com",
        "http://example.com/path?token=value",
        "http://example.com/path#fragment",
        "http://example.com/collector/v1/traces/more",
        " http://127.0.0.1:8799",
        "http://127.0.0.1:99999",
    ],
)
def test_rejects_invalid_catalyst_sdk_base_endpoint(
    tmp_path: Path,
    endpoint: str,
) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            f'[observation]\ncatalyst_sdk_base_endpoint = "{endpoint}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="catalyst_sdk_base_endpoint"):
        HaloConfig.load(tmp_path)


def test_rejects_legacy_desktop_otlp_endpoint_field(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            '[observation]\ndesktop_otlp_base_endpoint = "http://127.0.0.1:8799"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="not supported"):
        HaloConfig.load(tmp_path)


def test_rejects_runtime_path_escape(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config_path = tmp_path / ".shea/halo.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[runtime]\nworktrees = "../outside"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must stay under"):
        HaloConfig.load(tmp_path)


def test_rejects_runtime_path_outside_managed_shea_tree(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config_path = tmp_path / ".shea/halo.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[runtime]\nworktrees = "src"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="below the target .shea"):
        HaloConfig.load(tmp_path)


def test_rejects_unknown_schema(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("shea-halo/v1", "shea-halo/v2"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="schema_version"):
        HaloConfig.load(tmp_path)


def test_tracker_lifecycle_statuses_must_be_distinct(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "project_number = 10",
            'project_number = 10\nresearch_state = "Todo"\nready_state = "todo"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be distinct"):
        HaloConfig.load(tmp_path)


def test_rejects_observation_glob_escape(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'trace_globs = [".shea/artifacts/halo/traces/*.jsonl"]',
            'trace_globs = ["../outside/*.jsonl"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="observation glob"):
        HaloConfig.load(tmp_path)


def test_rejects_target_selected_integration_catalog(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            '[observation]\ncatalog_url = "https://example.com/frameworks.txt"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fixed by the Shea Halo protocol"):
        HaloConfig.load(tmp_path)


def test_require_candidate_traces_can_be_disabled_explicitly(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            "[observation]\nrequire_candidate_traces = false",
        ),
        encoding="utf-8",
    )

    assert HaloConfig.load(tmp_path).observation.require_candidate_traces is False


@pytest.mark.parametrize("value", ['"true"', "1"])
def test_require_candidate_traces_must_be_boolean(tmp_path: Path, value: str) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[observation]",
            f"[observation]\nrequire_candidate_traces = {value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="require_candidate_traces"):
        HaloConfig.load(tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        '"69"',
        "[0]",
        "[256]",
        "[true]",
        "[69, 1.5]",
    ],
)
def test_external_blocker_exit_codes_must_be_reserved_statuses(
    tmp_path: Path,
    value: str,
) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[experiments]",
            f"[experiments]\nexternal_blocker_exit_codes = {value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="external_blocker_exit_codes"):
        HaloConfig.load(tmp_path)


def test_complete_local_config_takes_precedence(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    shared = tmp_path / ".shea/halo.toml"
    local = tmp_path / ".shea/halo.local.toml"
    local.write_text(
        shared.read_text(encoding="utf-8")
        .replace("Alive24/FailureReport", "Alive24/LocalTarget")
        .replace(
            "[experiments]",
            '[experiments]\npass_env = ["OPENAI_API_KEY", "CATALYST_OTLP_TOKEN"]',
        ),
        encoding="utf-8",
    )

    config = HaloConfig.load(tmp_path)
    assert config.repository == "Alive24/LocalTarget"
    assert config.target_environment_variables == (
        "CATALYST_OTLP_TOKEN",
        "OPENAI_API_KEY",
    )


def test_shared_config_cannot_request_host_credentials(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[experiments]",
            '[experiments]\npass_env = ["OPENAI_API_KEY"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="only in ignored halo.local.toml"):
        HaloConfig.load(tmp_path)


def test_investigator_turn_budget_can_be_increased_within_bound(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[models]\ninvestigator_max_turns = 60\n",
        encoding="utf-8",
    )

    assert HaloConfig.load(tmp_path).investigator_max_turns == 60


@pytest.mark.parametrize("value", [1, 100])
def test_investigator_turn_budget_accepts_closed_interval_boundaries(
    tmp_path: Path,
    value: int,
) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n[models]\ninvestigator_max_turns = {value}\n",
        encoding="utf-8",
    )

    assert HaloConfig.load(tmp_path).investigator_max_turns == value


@pytest.mark.parametrize("value", ["0", "101", "true", "1.5", '"60"'])
def test_investigator_turn_budget_is_bounded(tmp_path: Path, value: str) -> None:
    write_target_config(tmp_path)
    path = tmp_path / ".shea/halo.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n[models]\ninvestigator_max_turns = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="investigator_max_turns"):
        HaloConfig.load(tmp_path)


def test_checked_in_failure_report_example_uses_the_three_stage_runtime_contract() -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "failure-report"

    config = HaloConfig.load(root)

    assert config.setup_commands == (
        ("pnpm", "install", "--lockfile-only"),
        ("pnpm", "install", "--frozen-lockfile"),
    )
    assert config.experiment_commands == (("pnpm", "run", "halo:trace-smoke"),)
    assert config.external_blocker_exit_codes == frozenset({69})
    assert config.verification_commands == (
        ("pnpm", "build"),
        ("pnpm", "check"),
        ("pnpm", "test"),
        ("pnpm", "format:check"),
    )
