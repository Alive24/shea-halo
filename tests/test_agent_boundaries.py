from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import write_target_config

from shea_halo.agent import (
    InvestigationError,
    Investigator,
    _repository_search_argv,
    _safe_patch_file,
    blocker_is_runtime_verified,
    candidate_evidence_references,
    canonical_trace_contract,
    configured_actions,
    load_desktop_reports,
    re_patch_paths,
    run_verification,
    target_environment,
    validate_candidate_synthesis,
)
from shea_halo.config import HaloConfig
from shea_halo.halo_engine import (
    HaloAnalysisArtifact,
    TraceDatasetManifest,
    TraceFile,
)
from shea_halo.models import (
    AgentDecision,
    Evidence,
    ExecutedAction,
    ExternalBlocker,
    InvestigationResult,
    Outcome,
    Verification,
)
from shea_halo.observation import (
    ObservationCheckpoint,
    SpanKindCount,
    TraceObservation,
    TraceValidation,
)


def test_agent_can_only_select_exact_operator_configured_actions(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)

    assert configured_actions(config) == (
        ("setup", ("python", "-m", "pip", "--version")),
        ("experiment", ("python", "-m", "pytest")),
        ("verification", ("python", "-m", "pytest")),
    )


def test_canonical_trace_guidance_comes_from_the_installed_public_engine_model() -> None:
    contract = canonical_trace_contract()

    assert '"title": "SpanRecord"' in contract
    assert '"trace_id"' in contract
    assert '"resource"' in contract
    assert '"attributes"' in contract


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

    verification, actions = run_verification(
        config,
        tmp_path,
        service_version="a" * 40,
    )
    [result] = verification
    [action] = actions

    assert result.status == "passed"
    assert action.stage == "verification"
    assert "stdout_sha256=" in result.evidence
    assert secret not in result.evidence
    assert "redacted" not in result.evidence


def test_target_environment_is_explicit_and_collector_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_target_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-do-not-pass")
    monkeypatch.delenv("CATALYST_OTLP_ENDPOINT", raising=False)

    environment = target_environment(HaloConfig.load(tmp_path))

    assert "OPENAI_API_KEY" not in environment
    assert environment["CATALYST_OTLP_ENDPOINT"] == "http://127.0.0.1:8799"
    assert environment["CATALYST_SERVICE_NAME"] == "failurereport"


def test_desktop_signal_url_override_is_normalized_for_the_catalyst_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    monkeypatch.setenv(
        "CATALYST_OTLP_ENDPOINT",
        "http://127.0.0.1:8799/v1/traces",
    )

    environment = target_environment(HaloConfig.load(tmp_path))

    assert environment["CATALYST_OTLP_ENDPOINT"] == "http://127.0.0.1:8799"


def test_only_experiment_environment_receives_allowlisted_runtime_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    secret = "sk-test-experiment-only"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = replace(
        HaloConfig.load(tmp_path),
        target_environment_variables=("OPENAI_API_KEY",),
    )

    assert "OPENAI_API_KEY" not in target_environment(config)
    assert (
        target_environment(
            config,
            include_runtime_credentials=True,
        )["OPENAI_API_KEY"]
        == secret
    )


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


def test_agent_cannot_patch_git_controls_or_ignored_branch_external_files(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)

    with pytest.raises(InvestigationError, match="Git control"):
        _safe_patch_file(tmp_path, ".gitignore")
    with pytest.raises(InvestigationError, match="ignored branch-external"):
        _safe_patch_file(tmp_path, "generated/instrumentation.js")

    assert _safe_patch_file(tmp_path, "src/instrumentation.ts") == (
        tmp_path / "src/instrumentation.ts"
    )


@pytest.mark.parametrize("query", ["--files", "--pre=sh"])
def test_repository_search_treats_option_shaped_queries_as_literals(
    tmp_path: Path,
    query: str,
) -> None:
    (tmp_path / "evidence.txt").write_text(f"{query}\n", encoding="utf-8")

    process = subprocess.run(
        _repository_search_argv(query),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert f"evidence.txt:1:{query}" in process.stdout


def test_desktop_report_is_plain_interoperability_artifact(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    report = tmp_path / ".shea/artifacts/halo/desktop/run-1/report.md"
    report.parent.mkdir(parents=True)
    report.write_text("# HALO Desktop report\n\nFinding.", encoding="utf-8")

    rendered = load_desktop_reports(HaloConfig.load(tmp_path))

    assert "desktop/run-1/report.md" in rendered
    assert "Finding." in rendered


def test_only_operator_reserved_experiment_status_verifies_a_blocker() -> None:
    decision = AgentDecision(
        outcome=Outcome.BLOCKED,
        summary="The collector is unavailable.",
        blocker=ExternalBlocker(
            category="service_unavailable",
            description="The configured collector rejected delivery.",
            required_action="Restore the collector.",
            failed_action_index=1,
        ),
    )
    action = ExecutedAction(
        index=1,
        kind="experiment",
        stage="exploration",
        command=["pnpm", "run", "halo:trace-smoke"],
        exit_code=69,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        service_version="halo-26-exploration",
    )

    assert blocker_is_runtime_verified(
        decision,
        [action],
        allowed_exit_codes=frozenset({69}),
    )
    assert not blocker_is_runtime_verified(
        decision,
        [action.model_copy(update={"exit_code": 1})],
        allowed_exit_codes=frozenset({69}),
    )


def _candidate_trace(revision: str) -> TraceObservation:
    return TraceObservation(
        label="worktree/.shea/artifacts/halo/traces/candidate.jsonl",
        sha256="d" * 64,
        size_bytes=200,
        span_count=2,
        trace_count=1,
        parented_span_count=1,
        span_kinds=(
            SpanKindCount(kind="AGENT", count=1),
            SpanKindCount(kind="TOOL", count=1),
        ),
        service_version_span_count=2,
        service_versions=(revision,),
    )


def _candidate_analysis(revision: str) -> HaloAnalysisArtifact:
    return HaloAnalysisArtifact(
        halo_engine_version="0.2.1",
        dataset=TraceDatasetManifest(
            files=[
                TraceFile(
                    path="workspace:.shea/artifacts/halo/traces/candidate.jsonl",
                    sha256="d" * 64,
                    span_count=2,
                )
            ],
            harness_revision=revision,
        ),
        prompt="Bounded HALO prompt.",
        report_path="<artifact-run>/report.md",
        events_path="<artifact-run>/events.jsonl",
        final_message="The candidate trace closes the observed hierarchy gap.",
    )


def test_terminal_candidate_synthesis_requires_exact_revision_bound_sources() -> None:
    revision = "1" * 40
    trace = _candidate_trace(revision)
    analysis = _candidate_analysis(revision)
    report_reference, trace_references = candidate_evidence_references(
        candidate_revision=revision,
        halo_analysis=analysis,
        candidate_traces=(trace,),
    )
    assert report_reference is not None

    valid = AgentDecision(
        outcome=Outcome.READY_FOR_TODO,
        summary="The exact candidate supports a handoff.",
        evidence=[
            Evidence(claim="HALO report.", source=report_reference),
            Evidence(claim="Candidate dataset.", source=trace_references[0]),
        ],
        todo_handoff="Adopt the candidate instrumentation in a separate branch.",
    )
    validate_candidate_synthesis(
        valid,
        candidate_revision=revision,
        halo_analysis=analysis,
        candidate_traces=(trace,),
    )

    with pytest.raises(InvestigationError, match="exact HALO report"):
        validate_candidate_synthesis(
            valid.model_copy(
                update={
                    "evidence": [
                        Evidence(claim="Old report.", source="halo-report:preliminary"),
                        Evidence(claim="Candidate dataset.", source=trace_references[0]),
                    ]
                }
            ),
            candidate_revision=revision,
            halo_analysis=analysis,
            candidate_traces=(trace,),
        )


def test_candidate_synthesis_rejects_a_report_larger_than_its_prompt_boundary() -> None:
    revision = "1" * 40
    analysis = _candidate_analysis(revision).model_copy(update={"final_message": "x" * 30_001})

    with pytest.raises(InvestigationError, match="bounded final-synthesis"):
        candidate_evidence_references(
            candidate_revision=revision,
            halo_analysis=analysis,
            candidate_traces=(_candidate_trace(revision),),
        )


async def test_candidate_synthesis_is_zero_tool_and_receives_exact_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    revision = "1" * 40
    trace = _candidate_trace(revision)
    analysis = _candidate_analysis(revision).model_copy(
        update={"final_message": "auth: [REDACTED]"}
    )
    report_reference, trace_references = candidate_evidence_references(
        candidate_revision=revision,
        halo_analysis=analysis,
        candidate_traces=(trace,),
    )
    assert report_reference is not None
    decision = AgentDecision(
        outcome=Outcome.NO_CHANGE,
        summary="The exact candidate disproves the proposed change.",
        evidence=[
            Evidence(claim="HALO report.", source=report_reference),
            Evidence(claim="Candidate dataset.", source=trace_references[0]),
        ],
    )
    captured: dict[str, object] = {}

    async def run(agent: object, prompt: str, **_kwargs: object) -> object:
        captured["tools"] = getattr(agent, "tools")
        captured["prompt"] = prompt
        return SimpleNamespace(final_output=decision)

    monkeypatch.setattr("shea_halo.agent.Runner.run", run)
    action = ExecutedAction(
        index=1,
        kind="experiment",
        stage="candidate_validation",
        command=["pnpm", "run", "halo:trace-smoke"],
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        service_version=revision,
    )
    verification = Verification(
        command=["pnpm", "test"],
        status="passed",
        evidence="exit_code=0",
    )
    checkpoint = ObservationCheckpoint(traces=(trace,))
    trace_validation = TraceValidation(
        checkpoint=checkpoint,
        candidate_traces=(trace,),
        halo_report_sha256=report_reference.split(":")[2].split("@")[0],
        halo_dataset_verified=True,
        halo_revision_verified=True,
        service_version_verified=True,
        candidate_revision=revision,
        complete=True,
    )

    result = await Investigator().synthesize_candidate(
        config=config,
        issue_number=26,
        issue_title="Improve Eve tracing",
        issue_body="Observe the current harness. token=ghp_abcdefghijklmnopqrstuvwxyz123456",
        issue_discussion="No additional discussion.",
        candidate_revision=revision,
        preliminary_result=InvestigationResult(
            outcome=Outcome.READY_FOR_TODO,
            summary="Preliminary claim that must be reconsidered.",
        ),
        halo_analysis=analysis,
        candidate_traces=(trace,),
        trace_validation=trace_validation,
        validation_actions=[action],
        verification=[verification],
        source_clean=True,
    )

    assert result.outcome == Outcome.NO_CHANGE
    assert captured["tools"] == []
    prompt = cast(str, captured["prompt"])
    assert revision in prompt
    assert trace.sha256 in prompt
    assert report_reference in prompt
    payload = json.loads(prompt.split("\n\n", 1)[1])
    assert payload["halo_engine"]["report"] == analysis.final_message
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in payload["issue"]["body"]
