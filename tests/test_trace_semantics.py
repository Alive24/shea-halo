from __future__ import annotations

import json
from pathlib import Path

import pytest
from engine.traces.models.canonical_span import SpanRecord

from shea_halo.halo_engine import (
    TraceDatasetManifest,
    _semantic_analysis_context,
    _trace_file,
)
from shea_halo.security import REDACTED, sanitize_persisted_data
from shea_halo.trace_semantics import (
    SemanticProjectionError,
    project_trace_semantics,
    semantic_receipt_sha256,
)

FIXTURES = Path(__file__).parent / "fixtures" / "eve-semantics"


def load_spans(name: str) -> list[SpanRecord]:
    return [
        SpanRecord.model_validate_json(line, strict=True)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_synthetic_public_aggregate_is_llm_only_and_provenance_preserving() -> None:
    receipt = project_trace_semantics(load_spans("synthetic-public-aggregate.jsonl"))

    assert receipt.span_count == 7
    assert receipt.llm_span_count == 3
    assert receipt.input_tokens == 498_161
    assert receipt.output_tokens == 22_392
    assert [agent.identity.value for agent in receipt.agents if agent.identity is not None] == [
        "failure-report-root",
        "codex",
    ]
    assert [
        (item.identity.value, item.input_tokens, item.output_tokens)
        for item in receipt.agent_rollups
    ] == [
        ("failure-report-root", 100_000, 5_000),
        ("codex", 398_161, 17_392),
    ]
    assert receipt.contributions[1].agent_path == (
        "0000000000000002",
        "0000000000000004",
    )
    assert receipt.contributions[1].attributed_agent_span_id == "0000000000000004"
    assert {source.source_key for source in receipt.contributions[0].span_kind.sources} == {
        "gen_ai.operation.name",
        "openinference.span.kind",
    }
    assert receipt.contributions[1].topology_sources[0].source_key == "parent_span_id"
    assert receipt.contributions[1].topology_sources[0].span_id == "0000000000000005"
    assert receipt.agents[1].parent_path_sources[0].span_id == "0000000000000004"
    assert {source.source_key for source in receipt.contributions[0].input_tokens.sources} == {
        "gen_ai.usage.input_tokens",
        "llm.token_count.prompt",
    }
    assert {source.source_key for source in receipt.contributions[1].input_tokens.sources} == {
        "$eve.input_tokens",
        "llm.token_count.prompt",
    }
    assert {source.source_key for source in receipt.contributions[2].input_tokens.sources} == {
        "ai.usage.inputTokens"
    }


def test_eve_enclosing_aggregate_is_not_a_token_contribution() -> None:
    receipt = project_trace_semantics(load_spans("synthetic-public-aggregate.jsonl"))

    assert "0000000000000001" not in {
        contribution.span_id for contribution in receipt.contributions
    }
    assert sum(item.input_tokens.value for item in receipt.contributions) == 498_161
    assert sum(item.output_tokens.value for item in receipt.contributions) == 22_392


def test_identical_duplicate_aliases_are_accepted_without_double_counting() -> None:
    receipt = project_trace_semantics(load_spans("identical-duplicate-aliases.jsonl"))

    assert receipt.input_tokens == 13
    assert receipt.output_tokens == 5
    [contribution] = receipt.contributions
    assert {source.source_key for source in contribution.input_tokens.sources} == {
        "$eve.input_tokens",
        "ai.usage.inputTokens",
        "ai.usage.promptTokens",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.prompt_tokens",
        "llm.token_count.prompt",
    }
    assert {source.source_key for source in contribution.output_tokens.sources} == {
        "$eve.output_tokens",
        "ai.usage.completionTokens",
        "ai.usage.outputTokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.usage.output_tokens",
        "llm.token_count.completion",
    }
    [agent] = receipt.agents
    assert agent.agent_name is not None
    assert len(agent.agent_name.sources) == 3


def test_missing_optional_model_and_agent_name_remain_absent() -> None:
    receipt = project_trace_semantics(load_spans("missing-optional-values.jsonl"))

    [agent] = receipt.agents
    [contribution] = receipt.contributions
    assert agent.agent_id is not None
    assert agent.agent_id.value == "id-only-agent"
    assert agent.agent_name is None
    assert contribution.requested_model is None
    assert contribution.response_model is None
    assert receipt.input_tokens == 8
    assert receipt.output_tokens == 2


def test_response_model_identity_is_distinct_from_requested_model_aliases() -> None:
    spans = load_spans("missing-optional-values.jsonl")
    spans[1] = spans[1].model_copy(
        update={
            "attributes": {
                **spans[1].attributes,
                "gen_ai.request.model": "requested-model",
                "llm.model_name": "requested-model",
                "gen_ai.response.model": "resolved-model-version",
            }
        }
    )

    [contribution] = project_trace_semantics(spans).contributions

    assert contribution.requested_model is not None
    assert contribution.requested_model.value == "requested-model"
    assert contribution.response_model is not None
    assert contribution.response_model.value == "resolved-model-version"
    assert contribution.response_model.sources[0].source_key == "gen_ai.response.model"


@pytest.mark.parametrize(
    ("fixture", "kind", "field"),
    [
        ("conflicting-aliases.jsonl", "alias_conflict", "input_tokens"),
        ("ambiguous-attribution.jsonl", "ambiguous_token_ownership", "parent_span_id"),
        (
            "parent-child-double-count.jsonl",
            "ambiguous_token_ownership",
            "span_kind",
        ),
    ],
)
def test_negative_fixtures_fail_closed_with_raw_value_free_evidence(
    fixture: str,
    kind: str,
    field: str,
) -> None:
    with pytest.raises(SemanticProjectionError) as raised:
        project_trace_semantics(load_spans(fixture))

    evidence = raised.value.evidence
    assert evidence.kind == kind
    assert evidence.field == field
    assert set(evidence.model_dump()) == {
        "schema_version",
        "kind",
        "field",
        "trace_id",
        "span_id",
        "source_keys",
        "related_span_ids",
    }


def test_conflicting_span_kind_aliases_fail_closed() -> None:
    spans = load_spans("missing-optional-values.jsonl")
    spans[1] = spans[1].model_copy(
        update={"attributes": {**spans[1].attributes, "gen_ai.operation.name": "execute_tool"}}
    )

    with pytest.raises(SemanticProjectionError) as raised:
        project_trace_semantics(spans)

    assert raised.value.evidence.kind == "alias_conflict"
    assert raised.value.evidence.field == "span_kind"
    assert raised.value.evidence.source_keys == (
        "gen_ai.operation.name",
        "openinference.span.kind",
    )


def test_incomplete_token_pair_and_inconsistent_total_fail_closed() -> None:
    spans = load_spans("missing-optional-values.jsonl")
    spans[1] = spans[1].model_copy(
        update={
            "attributes": {
                key: value
                for key, value in spans[1].attributes.items()
                if key != "llm.token_count.completion"
            }
        }
    )
    with pytest.raises(SemanticProjectionError) as incomplete:
        project_trace_semantics(spans)
    assert incomplete.value.evidence.kind == "incomplete_token_usage"

    spans = load_spans("missing-optional-values.jsonl")
    spans[1] = spans[1].model_copy(
        update={"attributes": {**spans[1].attributes, "llm.token_count.total": 99}}
    )
    with pytest.raises(SemanticProjectionError) as inconsistent:
        project_trace_semantics(spans)
    assert inconsistent.value.evidence.kind == "alias_conflict"
    assert inconsistent.value.evidence.field == "total_tokens"


@pytest.mark.parametrize("topology", ["missing-parent", "repeated-id"])
def test_incomplete_or_repeated_topology_fails_closed(topology: str) -> None:
    spans = load_spans("missing-optional-values.jsonl")
    if topology == "missing-parent":
        spans[1] = spans[1].model_copy(update={"parent_span_id": "9999999999999999"})
    else:
        spans[1] = spans[1].model_copy(update={"span_id": spans[0].span_id, "parent_span_id": ""})

    with pytest.raises(SemanticProjectionError) as raised:
        project_trace_semantics(spans)

    assert raised.value.evidence.kind == "invalid_topology"


def test_unknown_attributes_and_raw_span_payloads_do_not_enter_receipt() -> None:
    receipt = project_trace_semantics(load_spans("synthetic-public-aggregate.jsonl"))
    rendered = receipt.model_dump_json()

    assert "fixture.unknown_attribute" not in rendered
    assert "preserved only in canonical input" not in rendered
    assert "attributes" not in receipt.model_dump()
    assert len(semantic_receipt_sha256(receipt)) == 64


def test_trace_file_exposes_bounded_semantic_analysis_receipt() -> None:
    trace = _trace_file(FIXTURES / "synthetic-public-aggregate.jsonl")

    assert trace.semantic_receipt is not None
    assert trace.semantic_receipt.input_tokens == 498_161
    assert trace.semantic_receipt.output_tokens == 22_392
    assert trace.semantic_receipt_sha256 == semantic_receipt_sha256(trace.semantic_receipt)


def test_dataset_manifest_exposes_verified_totals_and_agent_identities() -> None:
    trace = _trace_file(FIXTURES / "synthetic-public-aggregate.jsonl")
    assert trace.semantic_receipt is not None

    dataset = TraceDatasetManifest(
        files=[trace],
        harness_revision="a" * 40,
        semantic_projection_complete=True,
        input_tokens=trace.semantic_receipt.input_tokens,
        output_tokens=trace.semantic_receipt.output_tokens,
        agent_identities=("codex", "failure-report-root"),
    )

    assert dataset.input_tokens == 498_161
    assert dataset.output_tokens == 22_392
    assert dataset.agent_identities == ("codex", "failure-report-root")
    context = json.loads(_semantic_analysis_context(dataset))
    assert context["input_tokens"] == 498_161
    assert context["output_tokens"] == 22_392
    assert context["files"][0]["receipt"]["contributions"][0]["input_tokens"]["sources"][0][
        "source_key"
    ] in {"gen_ai.usage.input_tokens", "llm.token_count.prompt"}
    assert "fixture.unknown_attribute" not in json.dumps(context)


def test_persistence_safety_keeps_known_numeric_rollups_but_redacts_credentials() -> None:
    sanitized = sanitize_persisted_data(
        {
            "input_tokens": 498_161,
            "output_tokens": 22_392,
            "api_token": 123456,
            "nested": {"access_token": "not-safe-to-persist"},
            "source_key": "llm.token_count.prompt",
            "unsafe_source_key": "sk-not-safe-to-persist",
        }
    )

    assert sanitized == {
        "input_tokens": 498_161,
        "output_tokens": 22_392,
        "api_token": REDACTED,
        "nested": {"access_token": REDACTED},
        "source_key": "llm.token_count.prompt",
        "unsafe_source_key": REDACTED,
    }
    receipt = project_trace_semantics(load_spans("identical-duplicate-aliases.jsonl"))
    receipt_data = receipt.model_dump(mode="json")
    assert sanitize_persisted_data(receipt_data) == receipt_data


def test_fixture_inventory_is_explicitly_synthetic_and_contains_no_sensitive_shapes() -> None:
    notice = " ".join((FIXTURES / "README.md").read_text(encoding="utf-8").casefold().split())
    all_fixtures = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(FIXTURES.glob("*.jsonl"))
    )
    records = [json.loads(line) for line in all_fixtures.splitlines() if line.strip()]

    assert "constructed test data" in notice
    assert "not copied, exported, or derived from the original trace" in notice
    assert "not evidence" in notice
    forbidden_keys = {
        "input.value",
        "output.value",
        "tool.arguments",
        "tool.result",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "http.url",
        "server.address",
        "authorization",
    }
    all_attribute_keys = {
        key
        for record in records
        for key in (
            set(record.get("attributes", {}))
            | set(record.get("resource", {}).get("attributes", {}))
        )
    }
    assert not all_attribute_keys.intersection(forbidden_keys)
    assert "Bearer " not in all_fixtures
    assert "://" not in all_fixtures
    assert "/Users/" not in all_fixtures
    assert "/home/" not in all_fixtures
