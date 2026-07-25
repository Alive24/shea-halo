from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shea_halo.security import PersistenceSafetyError, sanitize_persisted_text

if TYPE_CHECKING:
    from engine.traces.models.canonical_span import SpanRecord

__all__ = [
    "AgentAttribution",
    "AgentTokenRollup",
    "SemanticConflictEvidence",
    "SemanticInteger",
    "SemanticProjectionError",
    "SemanticSource",
    "SemanticString",
    "TokenContribution",
    "TraceSemanticReceipt",
    "project_trace_semantics",
    "semantic_receipt_sha256",
]

MAX_SEMANTIC_SPANS = 20_000
MAX_TOKEN_CONTRIBUTIONS = 2_000
MAX_AGENT_DEPTH = 64
MAX_SEMANTIC_VALUE_CHARACTERS = 256

_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")

SemanticSpanKind = Literal[
    "AGENT",
    "LLM",
    "TOOL",
    "CHAIN",
    "RETRIEVER",
    "EMBEDDING",
    "RERANKER",
    "GUARDRAIL",
    "EVALUATOR",
]
SemanticConflictKind = Literal[
    "alias_conflict",
    "invalid_alias_value",
    "incomplete_token_usage",
    "ambiguous_token_ownership",
    "incomplete_agent_attribution",
    "invalid_topology",
    "projection_limit",
]
SemanticField = Literal[
    "span_kind",
    "agent_id",
    "agent_name",
    "requested_model",
    "response_model",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "parent_span_id",
    "span_id",
]

_SPAN_KIND_KEYS = ("openinference.span.kind",)
_AGENT_ID_KEYS = ("agent.id", "gen_ai.agent.id")
_AGENT_NAME_KEYS = (
    "agent.name",
    "gen_ai.agent.name",
    "ai.telemetry.functionId",
    "resource.name",
)
_REQUESTED_MODEL_KEYS = (
    "llm.model_name",
    "gen_ai.request.model",
    "ai.model.id",
    "$eve.model",
)
_RESPONSE_MODEL_KEYS = ("gen_ai.response.model",)
_INPUT_TOKEN_KEYS = (
    "llm.token_count.prompt",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
    "ai.usage.inputTokens",
    "ai.usage.promptTokens",
    "$eve.input_tokens",
)
_OUTPUT_TOKEN_KEYS = (
    "llm.token_count.completion",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "ai.usage.outputTokens",
    "ai.usage.completionTokens",
    "$eve.output_tokens",
)
_TOTAL_TOKEN_KEYS = (
    "llm.token_count.total",
    "gen_ai.usage.total_tokens",
    "ai.usage.totalTokens",
)
_NON_AGGREGATE_TOKEN_KEYS = frozenset(
    (
        *(key for key in _INPUT_TOKEN_KEYS if not key.startswith("$eve.")),
        *(key for key in _OUTPUT_TOKEN_KEYS if not key.startswith("$eve.")),
        *_TOTAL_TOKEN_KEYS,
    )
)

_OPERATION_KINDS: dict[str, SemanticSpanKind] = {
    "invoke_agent": "AGENT",
    "chat": "LLM",
    "execute_tool": "TOOL",
    "agent_step": "CHAIN",
}
_AI_OPERATION_KINDS: dict[str, SemanticSpanKind] = {
    "ai.generateText": "AGENT",
    "ai.streamText": "AGENT",
    "ai.generateText.doGenerate": "LLM",
    "ai.streamText.doStream": "LLM",
    "ai.toolCall": "TOOL",
}
_NATIVE_NAME_KINDS: dict[str, SemanticSpanKind] = {
    "ai.eve.turn": "CHAIN",
    "invoke_agent": "AGENT",
    "execute_tool": "TOOL",
}
_SUPPORTED_SPAN_KINDS = frozenset(SemanticSpanKind.__args__)
_KNOWN_SOURCE_KEYS = frozenset(
    (
        *_SPAN_KIND_KEYS,
        *_AGENT_ID_KEYS,
        *_AGENT_NAME_KEYS,
        *_REQUESTED_MODEL_KEYS,
        *_RESPONSE_MODEL_KEYS,
        *_INPUT_TOKEN_KEYS,
        *_OUTPUT_TOKEN_KEYS,
        *_TOTAL_TOKEN_KEYS,
        "gen_ai.operation.name",
        "ai.operationId",
        "$eve.type",
        "name",
        "parent_span_id",
    )
)


def _canonical_trace_id(value: str) -> str:
    if not _TRACE_ID.fullmatch(value):
        raise ValueError("trace_id must be 32 hexadecimal characters")
    return value.lower()


def _canonical_span_id(value: str) -> str:
    if not _SPAN_ID.fullmatch(value):
        raise ValueError("span_id must be 16 hexadecimal characters")
    return value.lower()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticSource(_FrozenModel):
    trace_id: str
    span_id: str
    source_key: str = Field(min_length=1, max_length=128)

    @field_validator("trace_id")
    @classmethod
    def _valid_trace_id(cls, value: str) -> str:
        return _canonical_trace_id(value)

    @field_validator("span_id")
    @classmethod
    def _valid_span_id(cls, value: str) -> str:
        return _canonical_span_id(value)

    @field_validator("source_key")
    @classmethod
    def _supported_source_key(cls, value: str) -> str:
        if value not in _KNOWN_SOURCE_KEYS:
            raise ValueError("semantic source key is not a supported alias or topology field")
        return value


class SemanticString(_FrozenModel):
    value: str = Field(min_length=1, max_length=MAX_SEMANTIC_VALUE_CHARACTERS)
    sources: tuple[SemanticSource, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _stable_sources(self) -> SemanticString:
        if self.sources != tuple(sorted(set(self.sources), key=lambda item: item.source_key)):
            raise ValueError("semantic string sources must be unique and sorted")
        return self


class SemanticInteger(_FrozenModel):
    value: int = Field(ge=0)
    sources: tuple[SemanticSource, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _stable_sources(self) -> SemanticInteger:
        if self.sources != tuple(sorted(set(self.sources), key=lambda item: item.source_key)):
            raise ValueError("semantic integer sources must be unique and sorted")
        return self


class SemanticConflictEvidence(_FrozenModel):
    schema_version: Literal["shea-halo/semantic-conflict-v1"] = "shea-halo/semantic-conflict-v1"
    kind: SemanticConflictKind
    field: SemanticField
    trace_id: str
    span_id: str
    source_keys: tuple[str, ...] = Field(default=(), max_length=8)
    related_span_ids: tuple[str, ...] = Field(default=(), max_length=MAX_AGENT_DEPTH)

    @model_validator(mode="after")
    def _stable_evidence(self) -> SemanticConflictEvidence:
        if not _TRACE_ID.fullmatch(self.trace_id) or not _SPAN_ID.fullmatch(self.span_id):
            raise ValueError("semantic conflict evidence requires canonical trace/span identifiers")
        if self.source_keys != tuple(sorted(set(self.source_keys))):
            raise ValueError("semantic conflict source keys must be unique and sorted")
        if self.related_span_ids != tuple(sorted(set(self.related_span_ids))):
            raise ValueError("related span identifiers must be unique and sorted")
        return self


class SemanticProjectionError(ValueError):
    """Fail-closed semantic projection error with raw-value-free evidence."""

    def __init__(self, message: str, evidence: SemanticConflictEvidence):
        super().__init__(message)
        self.evidence = evidence


class AgentAttribution(_FrozenModel):
    trace_id: str
    span_id: str
    parent_agent_span_id: str | None = None
    parent_path_sources: tuple[SemanticSource, ...] = Field(
        default=(),
        max_length=MAX_AGENT_DEPTH,
    )
    span_kind: SemanticString
    agent_id: SemanticString | None = None
    agent_name: SemanticString | None = None

    @field_validator("trace_id")
    @classmethod
    def _valid_trace_id(cls, value: str) -> str:
        return _canonical_trace_id(value)

    @field_validator("span_id")
    @classmethod
    def _valid_span_id(cls, value: str) -> str:
        return _canonical_span_id(value)

    @field_validator("parent_agent_span_id")
    @classmethod
    def _valid_parent_agent_span_id(cls, value: str | None) -> str | None:
        return _canonical_span_id(value) if value is not None else None

    @model_validator(mode="after")
    def _identity_sources_match_agent(self) -> AgentAttribution:
        for identity in (self.span_kind, self.agent_id, self.agent_name):
            if identity is not None and any(
                source.trace_id != self.trace_id or source.span_id != self.span_id
                for source in identity.sources
            ):
                raise ValueError("agent identity provenance must come from its source span")
        if bool(self.parent_agent_span_id) != bool(self.parent_path_sources):
            raise ValueError(
                "parent agent attribution and its topology sources must both be present or absent"
            )
        if self.parent_path_sources:
            if self.parent_path_sources[0].span_id != self.span_id or any(
                source.trace_id != self.trace_id or source.source_key != "parent_span_id"
                for source in self.parent_path_sources
            ):
                raise ValueError("parent agent topology provenance is inconsistent")
        return self

    @property
    def identity(self) -> SemanticString | None:
        return self.agent_id or self.agent_name


class TokenContribution(_FrozenModel):
    trace_id: str
    span_id: str
    attributed_agent_span_id: str
    agent_path: tuple[str, ...] = Field(min_length=1, max_length=MAX_AGENT_DEPTH)
    topology_sources: tuple[SemanticSource, ...] = Field(
        min_length=1,
        max_length=MAX_AGENT_DEPTH,
    )
    span_kind: SemanticString
    requested_model: SemanticString | None = None
    response_model: SemanticString | None = None
    input_tokens: SemanticInteger
    output_tokens: SemanticInteger

    @field_validator("trace_id")
    @classmethod
    def _valid_trace_id(cls, value: str) -> str:
        return _canonical_trace_id(value)

    @field_validator("span_id", "attributed_agent_span_id")
    @classmethod
    def _valid_span_id(cls, value: str) -> str:
        return _canonical_span_id(value)

    @field_validator("agent_path")
    @classmethod
    def _valid_agent_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_canonical_span_id(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("agent attribution path cannot repeat a span")
        return normalized

    @model_validator(mode="after")
    def _provenance_matches_contribution(self) -> TokenContribution:
        if self.agent_path[-1] != self.attributed_agent_span_id:
            raise ValueError("attributed agent must be the nearest agent in the path")
        for semantic_value in (
            self.span_kind,
            self.requested_model,
            self.response_model,
            self.input_tokens,
            self.output_tokens,
        ):
            if semantic_value is not None and any(
                source.trace_id != self.trace_id or source.span_id != self.span_id
                for source in semantic_value.sources
            ):
                raise ValueError("contribution provenance must come from its LLM source span")
        if self.topology_sources[0].span_id != self.span_id or any(
            source.trace_id != self.trace_id or source.source_key != "parent_span_id"
            for source in self.topology_sources
        ):
            raise ValueError("contribution topology provenance is inconsistent")
        return self


class AgentTokenRollup(_FrozenModel):
    trace_id: str
    agent_span_id: str
    identity: SemanticString
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    contribution_span_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_TOKEN_CONTRIBUTIONS)

    @field_validator("trace_id")
    @classmethod
    def _valid_trace_id(cls, value: str) -> str:
        return _canonical_trace_id(value)

    @field_validator("agent_span_id")
    @classmethod
    def _valid_agent_span_id(cls, value: str) -> str:
        return _canonical_span_id(value)

    @field_validator("contribution_span_ids")
    @classmethod
    def _stable_contributions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_canonical_span_id(item) for item in value)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("rollup contribution span identifiers must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def _identity_sources_match_agent(self) -> AgentTokenRollup:
        if any(
            source.trace_id != self.trace_id or source.span_id != self.agent_span_id
            for source in self.identity.sources
        ):
            raise ValueError("rollup identity provenance must come from its agent span")
        return self


class TraceSemanticReceipt(_FrozenModel):
    """Bounded, raw-payload-free evidence for deterministic trace attribution."""

    schema_version: Literal["shea-halo/trace-semantics-v1"] = "shea-halo/trace-semantics-v1"
    span_count: int = Field(gt=0, le=MAX_SEMANTIC_SPANS)
    llm_span_count: int = Field(ge=0, le=MAX_SEMANTIC_SPANS)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    agents: tuple[AgentAttribution, ...] = Field(default=(), max_length=MAX_SEMANTIC_SPANS)
    contributions: tuple[TokenContribution, ...] = Field(
        default=(),
        max_length=MAX_TOKEN_CONTRIBUTIONS,
    )
    agent_rollups: tuple[AgentTokenRollup, ...] = Field(
        default=(),
        max_length=MAX_SEMANTIC_SPANS,
    )

    @model_validator(mode="after")
    def _consistent_rollup(self) -> TraceSemanticReceipt:
        if self.llm_span_count > self.span_count:
            raise ValueError("LLM span count cannot exceed total span count")
        if sum(item.input_tokens.value for item in self.contributions) != self.input_tokens:
            raise ValueError("input token total must equal its LLM contributions")
        if sum(item.output_tokens.value for item in self.contributions) != self.output_tokens:
            raise ValueError("output token total must equal its LLM contributions")
        if sum(item.input_tokens for item in self.agent_rollups) != self.input_tokens:
            raise ValueError("agent input rollups must equal the trace input total")
        if sum(item.output_tokens for item in self.agent_rollups) != self.output_tokens:
            raise ValueError("agent output rollups must equal the trace output total")
        agent_keys = [(item.trace_id, item.span_id) for item in self.agents]
        contribution_keys = [(item.trace_id, item.span_id) for item in self.contributions]
        rollup_keys = [(item.trace_id, item.agent_span_id) for item in self.agent_rollups]
        if agent_keys != sorted(set(agent_keys)):
            raise ValueError("receipt agents must be unique and sorted")
        if contribution_keys != sorted(set(contribution_keys)):
            raise ValueError("receipt contributions must be unique and sorted")
        if rollup_keys != sorted(set(rollup_keys)):
            raise ValueError("receipt agent rollups must be unique and sorted")
        agent_by_key = {(item.trace_id, item.span_id): item for item in self.agents}
        contributions_by_agent: dict[tuple[str, str], list[TokenContribution]] = defaultdict(list)
        for contribution in self.contributions:
            path_agents = [
                agent_by_key.get((contribution.trace_id, span_id))
                for span_id in contribution.agent_path
            ]
            if any(agent is None for agent in path_agents):
                raise ValueError("contribution agent path must reference receipt agents")
            for parent, child in zip(path_agents, path_agents[1:], strict=False):
                if parent is None or child is None or child.parent_agent_span_id != parent.span_id:
                    raise ValueError("contribution agent path must follow the agent hierarchy")
            contributions_by_agent[
                (contribution.trace_id, contribution.attributed_agent_span_id)
            ].append(contribution)
        for rollup in self.agent_rollups:
            contributions = contributions_by_agent.get(
                (rollup.trace_id, rollup.agent_span_id),
                [],
            )
            if rollup.contribution_span_ids != tuple(item.span_id for item in contributions):
                raise ValueError("agent rollup must cite exactly its attributed contributions")
            if rollup.input_tokens != sum(item.input_tokens.value for item in contributions):
                raise ValueError("agent input rollup must equal its attributed contributions")
            if rollup.output_tokens != sum(item.output_tokens.value for item in contributions):
                raise ValueError("agent output rollup must equal its attributed contributions")
        return self


class _ProjectedSpan:
    def __init__(
        self,
        *,
        span: SpanRecord,
        kind: SemanticString | None,
        agent_id: SemanticString | None,
        agent_name: SemanticString | None,
        requested_model: SemanticString | None,
        response_model: SemanticString | None,
        input_tokens: SemanticInteger | None,
        output_tokens: SemanticInteger | None,
    ):
        self.span = span
        self.kind = kind
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.requested_model = requested_model
        self.response_model = response_model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _source(span: SpanRecord, key: str) -> SemanticSource:
    return SemanticSource(trace_id=span.trace_id, span_id=span.span_id, source_key=key)


def _conflict(
    span: SpanRecord,
    *,
    kind: SemanticConflictKind,
    field: SemanticField,
    source_keys: Iterable[str] = (),
    related_span_ids: Iterable[str] = (),
    message: str,
) -> SemanticProjectionError:
    return SemanticProjectionError(
        message,
        SemanticConflictEvidence(
            kind=kind,
            field=field,
            trace_id=span.trace_id.lower(),
            span_id=span.span_id.lower(),
            source_keys=tuple(sorted(set(source_keys))),
            related_span_ids=tuple(sorted(set(related_span_ids))),
        ),
    )


def _safe_string_value(span: SpanRecord, field: SemanticField, key: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _conflict(
            span,
            kind="invalid_alias_value",
            field=field,
            source_keys=(key,),
            message=f"{field} alias must be a string",
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_SEMANTIC_VALUE_CHARACTERS:
        raise _conflict(
            span,
            kind="invalid_alias_value",
            field=field,
            source_keys=(key,),
            message=f"{field} alias exceeds the bounded semantic value limit",
        )
    try:
        sanitized = sanitize_persisted_text(normalized)
    except PersistenceSafetyError:
        raise _conflict(
            span,
            kind="invalid_alias_value",
            field=field,
            source_keys=(key,),
            message=f"{field} alias is unsafe for a semantic receipt",
        ) from None
    if sanitized != normalized:
        raise _conflict(
            span,
            kind="invalid_alias_value",
            field=field,
            source_keys=(key,),
            message=f"{field} alias cannot be preserved safely",
        )
    return normalized


def _resolve_string(
    span: SpanRecord,
    attributes: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    field: SemanticField,
) -> SemanticString | None:
    populated: list[tuple[str, str]] = []
    for key in keys:
        normalized = _safe_string_value(span, field, key, attributes.get(key))
        if normalized is not None:
            populated.append((key, normalized))
    if not populated:
        return None
    values = {value for _, value in populated}
    if len(values) != 1:
        raise _conflict(
            span,
            kind="alias_conflict",
            field=field,
            source_keys=(key for key, _ in populated),
            message=f"equivalent {field} aliases disagree",
        )
    return SemanticString(
        value=populated[0][1],
        sources=tuple(
            sorted((_source(span, key) for key, _ in populated), key=lambda x: x.source_key)
        ),
    )


def _resolve_integer(
    span: SpanRecord,
    attributes: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    field: SemanticField,
) -> SemanticInteger | None:
    populated: list[tuple[str, int]] = []
    for key in keys:
        value = attributes.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _conflict(
                span,
                kind="invalid_alias_value",
                field=field,
                source_keys=(key,),
                message=f"{field} alias must be a non-negative integer",
            )
        populated.append((key, value))
    if not populated:
        return None
    values = {value for _, value in populated}
    if len(values) != 1:
        raise _conflict(
            span,
            kind="alias_conflict",
            field=field,
            source_keys=(key for key, _ in populated),
            message=f"equivalent {field} aliases disagree",
        )
    return SemanticInteger(
        value=populated[0][1],
        sources=tuple(
            sorted((_source(span, key) for key, _ in populated), key=lambda x: x.source_key)
        ),
    )


def _kind_candidates(span: SpanRecord) -> list[tuple[str, SemanticSpanKind]]:
    attributes = span.attributes
    candidates: list[tuple[str, SemanticSpanKind]] = []
    explicit = attributes.get(_SPAN_KIND_KEYS[0])
    if explicit is not None and explicit != "":
        if not isinstance(explicit, str) or explicit.strip().upper() not in _SUPPORTED_SPAN_KINDS:
            raise _conflict(
                span,
                kind="invalid_alias_value",
                field="span_kind",
                source_keys=_SPAN_KIND_KEYS,
                message="openinference span kind is unsupported",
            )
        candidates.append((_SPAN_KIND_KEYS[0], explicit.strip().upper()))  # type: ignore[arg-type]
    operation = attributes.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation in _OPERATION_KINDS:
        candidates.append(("gen_ai.operation.name", _OPERATION_KINDS[operation]))
    ai_operation = attributes.get("ai.operationId")
    if isinstance(ai_operation, str) and ai_operation in _AI_OPERATION_KINDS:
        candidates.append(("ai.operationId", _AI_OPERATION_KINDS[ai_operation]))
    if span.name in _NATIVE_NAME_KINDS:
        candidates.append(("name", _NATIVE_NAME_KINDS[span.name]))
    if attributes.get("$eve.type") == "turn":
        candidates.append(("$eve.type", "CHAIN"))
    return candidates


def _resolve_kind(span: SpanRecord) -> SemanticString | None:
    candidates = _kind_candidates(span)
    if not candidates:
        return None
    if len({kind for _, kind in candidates}) != 1:
        raise _conflict(
            span,
            kind="alias_conflict",
            field="span_kind",
            source_keys=(key for key, _ in candidates),
            message="equivalent span-kind signals disagree",
        )
    return SemanticString(
        value=candidates[0][1],
        sources=tuple(
            sorted((_source(span, key) for key, _ in candidates), key=lambda item: item.source_key)
        ),
    )


def _combined_attributes(span: SpanRecord) -> dict[str, Any]:
    attributes = dict(span.resource.attributes)
    attributes.update(span.attributes)
    return attributes


def _validate_topology(spans: tuple[SpanRecord, ...]) -> dict[tuple[str, str], SpanRecord]:
    by_identity: dict[tuple[str, str], SpanRecord] = {}
    parent_links: dict[tuple[str, str], tuple[str, str]] = {}
    for span in spans:
        trace_id = span.trace_id.lower()
        span_id = span.span_id.lower()
        if not _TRACE_ID.fullmatch(span.trace_id) or not _SPAN_ID.fullmatch(span.span_id):
            raise _conflict(
                span,
                kind="invalid_topology",
                field="span_id",
                message="semantic projection requires canonical trace/span identifiers",
            )
        identity = (trace_id, span_id)
        if identity in by_identity:
            raise _conflict(
                span,
                kind="invalid_topology",
                field="span_id",
                related_span_ids=(span_id,),
                message="semantic projection rejects repeated span identifiers",
            )
        by_identity[identity] = span
        if span.parent_span_id:
            if not _SPAN_ID.fullmatch(span.parent_span_id):
                raise _conflict(
                    span,
                    kind="invalid_topology",
                    field="parent_span_id",
                    message="semantic projection requires canonical parent span identifiers",
                )
            parent = (trace_id, span.parent_span_id.lower())
            if parent == identity:
                raise _conflict(
                    span,
                    kind="invalid_topology",
                    field="parent_span_id",
                    related_span_ids=(span_id,),
                    message="semantic projection rejects self-parenting spans",
                )
            parent_links[identity] = parent
    for identity, parent in parent_links.items():
        if parent not in by_identity:
            span = by_identity[identity]
            raise _conflict(
                span,
                kind="invalid_topology",
                field="parent_span_id",
                related_span_ids=(parent[1],),
                message="semantic projection rejects spans whose parent is unavailable",
            )
    for identity in parent_links:
        visited: set[tuple[str, str]] = set()
        cursor = identity
        while cursor in parent_links:
            if cursor in visited:
                span = by_identity[identity]
                raise _conflict(
                    span,
                    kind="invalid_topology",
                    field="parent_span_id",
                    related_span_ids=(item[1] for item in visited),
                    message="semantic projection rejects parent cycles",
                )
            visited.add(cursor)
            cursor = parent_links[cursor]
            if len(visited) > MAX_AGENT_DEPTH:
                span = by_identity[identity]
                raise _conflict(
                    span,
                    kind="projection_limit",
                    field="parent_span_id",
                    related_span_ids=(item[1] for item in visited),
                    message="semantic projection exceeds the bounded topology depth",
                )
    return by_identity


def project_trace_semantics(spans: Iterable[SpanRecord]) -> TraceSemanticReceipt:
    """Normalize supported native aliases and aggregate only attributable LLM usage."""

    source_spans = tuple(spans)
    if not source_spans:
        raise ValueError("semantic projection requires at least one span")
    if len(source_spans) > MAX_SEMANTIC_SPANS:
        span = source_spans[0]
        raise _conflict(
            span,
            kind="projection_limit",
            field="span_id",
            message="trace exceeds the bounded semantic span limit",
        )
    _validate_topology(source_spans)
    projected: dict[tuple[str, str], _ProjectedSpan] = {}
    agents: list[AgentAttribution] = []

    for span in source_spans:
        attributes = _combined_attributes(span)
        kind = _resolve_kind(span)
        agent_id = _resolve_string(span, attributes, _AGENT_ID_KEYS, field="agent_id")
        agent_name = _resolve_string(span, attributes, _AGENT_NAME_KEYS, field="agent_name")
        requested_model = _resolve_string(
            span,
            attributes,
            _REQUESTED_MODEL_KEYS,
            field="requested_model",
        )
        response_model = _resolve_string(
            span,
            attributes,
            _RESPONSE_MODEL_KEYS,
            field="response_model",
        )
        input_tokens = _resolve_integer(
            span,
            attributes,
            _INPUT_TOKEN_KEYS,
            field="input_tokens",
        )
        output_tokens = _resolve_integer(
            span,
            attributes,
            _OUTPUT_TOKEN_KEYS,
            field="output_tokens",
        )
        total_tokens = _resolve_integer(
            span,
            attributes,
            _TOTAL_TOKEN_KEYS,
            field="total_tokens",
        )
        populated_token_keys = {
            key
            for key in (*_INPUT_TOKEN_KEYS, *_OUTPUT_TOKEN_KEYS, *_TOTAL_TOKEN_KEYS)
            if attributes.get(key) is not None
        }
        kind_value = kind.value if kind is not None else None
        if kind_value != "LLM" and populated_token_keys.intersection(_NON_AGGREGATE_TOKEN_KEYS):
            raise _conflict(
                span,
                kind="ambiguous_token_ownership",
                field="span_kind",
                source_keys=populated_token_keys,
                message="non-LLM span exposes per-call token aliases",
            )
        if kind_value == "LLM":
            if (input_tokens is None) != (output_tokens is None):
                raise _conflict(
                    span,
                    kind="incomplete_token_usage",
                    field="input_tokens" if input_tokens is None else "output_tokens",
                    source_keys=populated_token_keys,
                    message="LLM token usage must include both input and output counts",
                )
            if total_tokens is not None:
                if input_tokens is None or output_tokens is None:
                    raise _conflict(
                        span,
                        kind="incomplete_token_usage",
                        field="total_tokens",
                        source_keys=populated_token_keys,
                        message="LLM total tokens require input and output token counts",
                    )
                if total_tokens.value != input_tokens.value + output_tokens.value:
                    raise _conflict(
                        span,
                        kind="alias_conflict",
                        field="total_tokens",
                        source_keys=populated_token_keys,
                        message="LLM total tokens disagree with input plus output",
                    )
        identity = (span.trace_id.lower(), span.span_id.lower())
        projected[identity] = _ProjectedSpan(
            span=span,
            kind=kind,
            agent_id=agent_id,
            agent_name=agent_name,
            requested_model=requested_model,
            response_model=response_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    agent_by_identity = {
        identity: item
        for identity, item in projected.items()
        if item.kind is not None and item.kind.value == "AGENT"
    }
    for identity, item in sorted(agent_by_identity.items()):
        assert item.kind is not None
        parent_agent_span_id: str | None = None
        traversed_sources: list[SemanticSource] = []
        child_span = item.span
        cursor = item.span.parent_span_id.lower() if item.span.parent_span_id else ""
        while cursor:
            traversed_sources.append(_source(child_span, "parent_span_id"))
            parent = projected[(identity[0], cursor)]
            if parent.kind is not None and parent.kind.value == "AGENT":
                parent_agent_span_id = parent.span.span_id.lower()
                break
            child_span = parent.span
            cursor = parent.span.parent_span_id.lower() if parent.span.parent_span_id else ""
        agents.append(
            AgentAttribution(
                trace_id=identity[0],
                span_id=identity[1],
                parent_agent_span_id=parent_agent_span_id,
                parent_path_sources=(
                    tuple(traversed_sources) if parent_agent_span_id is not None else ()
                ),
                span_kind=item.kind,
                agent_id=item.agent_id,
                agent_name=item.agent_name,
            )
        )

    contributions: list[TokenContribution] = []
    for identity, item in sorted(projected.items()):
        if (
            item.kind is None
            or item.kind.value != "LLM"
            or item.input_tokens is None
            or item.output_tokens is None
        ):
            continue
        agent_path: list[str] = []
        topology_sources: list[SemanticSource] = []
        child_span = item.span
        cursor = item.span.parent_span_id.lower() if item.span.parent_span_id else ""
        while cursor:
            topology_sources.append(_source(child_span, "parent_span_id"))
            parent_identity = (identity[0], cursor)
            parent = projected[parent_identity]
            if parent.kind is not None and parent.kind.value == "AGENT":
                if parent.agent_id is None and parent.agent_name is None:
                    raise _conflict(
                        item.span,
                        kind="incomplete_agent_attribution",
                        field="agent_name",
                        related_span_ids=(parent.span.span_id,),
                        message="token-owning LLM has an unidentified agent ancestor",
                    )
                agent_path.append(parent.span.span_id.lower())
            child_span = parent.span
            cursor = parent.span.parent_span_id.lower() if parent.span.parent_span_id else ""
        if not agent_path:
            raise _conflict(
                item.span,
                kind="ambiguous_token_ownership",
                field="parent_span_id",
                message="token-owning LLM has no agent ancestor",
            )
        if len(contributions) >= MAX_TOKEN_CONTRIBUTIONS:
            raise _conflict(
                item.span,
                kind="projection_limit",
                field="input_tokens",
                message="trace exceeds the bounded token-contribution limit",
            )
        contributions.append(
            TokenContribution(
                trace_id=identity[0],
                span_id=identity[1],
                attributed_agent_span_id=agent_path[0],
                agent_path=tuple(reversed(agent_path)),
                topology_sources=tuple(topology_sources),
                span_kind=item.kind,
                requested_model=item.requested_model,
                response_model=item.response_model,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
            )
        )

    agent_models = {(item.trace_id, item.span_id): item for item in agents}
    contributions_by_agent: dict[tuple[str, str], list[TokenContribution]] = defaultdict(list)
    for contribution in contributions:
        contributions_by_agent[
            (contribution.trace_id, contribution.attributed_agent_span_id)
        ].append(contribution)
    agent_rollups: list[AgentTokenRollup] = []
    for identity, items in sorted(contributions_by_agent.items()):
        agent = agent_models[identity]
        agent_identity = agent.identity
        if agent_identity is None:
            # The ancestor validation above makes this unreachable, but keep the
            # persisted rollup contract fail closed if construction changes.
            raise ValueError("attributed agent does not expose a stable identity")
        agent_rollups.append(
            AgentTokenRollup(
                trace_id=identity[0],
                agent_span_id=identity[1],
                identity=agent_identity,
                input_tokens=sum(item.input_tokens.value for item in items),
                output_tokens=sum(item.output_tokens.value for item in items),
                contribution_span_ids=tuple(item.span_id for item in items),
            )
        )

    return TraceSemanticReceipt(
        span_count=len(source_spans),
        llm_span_count=sum(
            item.kind is not None and item.kind.value == "LLM" for item in projected.values()
        ),
        input_tokens=sum(item.input_tokens.value for item in contributions),
        output_tokens=sum(item.output_tokens.value for item in contributions),
        agents=tuple(agents),
        contributions=tuple(contributions),
        agent_rollups=tuple(agent_rollups),
    )


def semantic_receipt_sha256(receipt: TraceSemanticReceipt) -> str:
    """Return a stable digest for callers that need to bind safe receipt evidence."""

    return hashlib.sha256(receipt.model_dump_json().encode("utf-8")).hexdigest()
