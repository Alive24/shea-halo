from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import write_target_config

from shea_halo.agent import (
    InvestigationError,
    _safe_patch_file,
    configured_actions,
    load_desktop_reports,
    re_patch_paths,
    run_verification,
    target_environment,
)
from shea_halo.config import HaloConfig


def test_agent_can_only_select_exact_operator_configured_actions(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)

    assert configured_actions(config) == (
        ("experiment", ("python", "-m", "pytest")),
        ("verification", ("python", "-m", "pytest")),
    )


def test_module_entrypoint_rejects_action_commands() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "shea_halo", "analyze"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode != 0
    assert "accepts no action arguments" in process.stderr


def test_verification_never_persists_process_output_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_target_config(tmp_path)
    secret = "sk-test-do-not-persist"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = replace(
        HaloConfig.load(tmp_path),
        target_environment_variables=("OPENAI_API_KEY",),
        verification_commands=(
            (
                sys.executable,
                "-c",
                "import os; print(os.getenv('OPENAI_API_KEY', 'redacted'))",
            ),
        ),
    )

    [result] = run_verification(config, tmp_path)

    assert result.status == "passed"
    assert "stdout_sha256=" in result.evidence
    assert secret not in result.evidence
    assert "redacted" not in result.evidence


def test_target_environment_is_explicit_and_desktop_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_target_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-pass")
    monkeypatch.delenv("CATALYST_OTLP_ENDPOINT", raising=False)

    environment = target_environment(HaloConfig.load(tmp_path))

    assert "OPENAI_API_KEY" not in environment
    assert environment["CATALYST_OTLP_ENDPOINT"] == "http://127.0.0.1:8799"
    assert environment["CATALYST_SERVICE_NAME"] == "failurereport"


def test_patch_paths_extract_unified_diff_targets() -> None:
    patch = """\
--- /dev/null
+++ b/eve/agent/instrumentation.ts
@@ -0,0 +1 @@
+export {};
"""
    assert re_patch_paths(patch) == ["eve/agent/instrumentation.ts"]


def test_agent_cannot_forge_trace_or_desktop_evidence_with_a_patch(tmp_path: Path) -> None:
    with pytest.raises(InvestigationError, match="cannot write Shea runtime"):
        _safe_patch_file(
            tmp_path,
            ".shea/artifacts/halo/traces/forged.jsonl",
        )


def test_agent_rejects_extended_rename_that_hides_a_runtime_evidence_target() -> None:
    patch = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-before
+after
diff --git a/trace.jsonl b/.shea/artifacts/halo/traces/forged.jsonl
similarity index 100%
rename from trace.jsonl
rename to .shea/artifacts/halo/traces/forged.jsonl
"""

    with pytest.raises(InvestigationError, match="rename"):
        re_patch_paths(patch)


def test_agent_rejects_git_quoted_paths_that_hide_runtime_evidence() -> None:
    patch = """\
diff --git a/report.md "b/\\056shea/artifacts/halo/desktop/run/report.md"
--- a/report.md
+++ "b/\\056shea/artifacts/halo/desktop/run/report.md"
@@ -1 +1 @@
-before
+forged
"""

    with pytest.raises(InvestigationError, match="quoted or escaped"):
        re_patch_paths(patch)


def test_desktop_report_is_plain_interoperability_artifact(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    report = tmp_path / ".shea/artifacts/halo/desktop/run-1/report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# HALO Desktop report\n\nFinding.", encoding="utf-8")

    rendered = load_desktop_reports(HaloConfig.load(tmp_path))

    assert "desktop/run-1/report.md" in rendered
    assert "Finding." in rendered
