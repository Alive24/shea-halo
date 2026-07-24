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
    assert config.experiment_commands == (("python", "-m", "pytest"),)
    assert config.target_environment_variables == ()
    assert config.verification_commands == (("python", "-m", "pytest"),)


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
