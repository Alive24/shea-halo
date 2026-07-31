from __future__ import annotations

from typing import TypeGuard

import httpx
import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import APIStatusError

from shea_halo.provider import (
    ApiMode,
    OpenAIModelBootstrap,
    ProviderApiModeError,
    ProviderAvailabilityBlockedError,
    ProviderAvailabilityPolicy,
    ProviderBlockerClassification,
)


def _is_api_mode(value: str) -> TypeGuard[ApiMode]:
    return value in {"responses", "chat_completions"}


@pytest.mark.parametrize(
    ("api_mode", "expected_model_type"),
    [
        ("responses", OpenAIResponsesModel),
        ("chat_completions", OpenAIChatCompletionsModel),
    ],
)
def test_bootstrap_selects_the_configured_agents_sdk_model(
    api_mode: str,
    expected_model_type: type[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-runtime-key")
    assert _is_api_mode(api_mode)

    model = OpenAIModelBootstrap(api_mode).provider().get_model("gpt-test")

    assert isinstance(model, expected_model_type)


def test_bootstrap_configures_the_public_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[str] = []
    monkeypatch.setattr(
        "shea_halo.provider.set_default_openai_api",
        configured.append,
    )

    OpenAIModelBootstrap("chat_completions").configure_sdk_default()

    assert configured == ["chat_completions"]


def test_bootstrap_disables_openai_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-runtime-key")

    provider = OpenAIModelBootstrap("responses").provider()

    assert getattr(provider, "_client").max_retries == 0


def test_unsupported_route_becomes_a_safe_structured_error() -> None:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses?api_key=secret-runtime-key",
    )
    error = APIStatusError(
        "route missing and token=secret-runtime-key",
        response=httpx.Response(404, request=request),
        body=None,
    )

    with pytest.raises(ProviderApiModeError) as raised:
        OpenAIModelBootstrap("responses").reraise_if_unsupported(error)

    assert raised.value.api_mode == "responses"
    assert raised.value.status_code == 404
    assert "secret-runtime-key" not in str(raised.value)
    assert "provider.invalid" not in str(raised.value)


def test_non_route_status_is_not_reclassified() -> None:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    error = APIStatusError(
        "provider failed",
        response=httpx.Response(500, request=request),
        body=None,
    )

    OpenAIModelBootstrap("responses").reraise_if_unsupported(error)


def _provider_error(
    *,
    status_code: int = 429,
    headers: dict[str, str] | None = None,
    body: object = None,
) -> APIStatusError:
    request = httpx.Request("POST", "https://provider.invalid/v1/responses")
    return APIStatusError(
        "provider detail that must never be persisted",
        response=httpx.Response(status_code, headers=headers, request=request),
        body=body,
    )


async def test_provider_policy_shares_two_short_throttle_retries_across_operations() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    policy = ProviderAvailabilityPolicy(sleep=sleep)
    attempts: list[str] = []

    async def investigator() -> str:
        attempts.append("investigator")
        if len(attempts) == 1:
            raise _provider_error(headers={"retry-after": "10"})
        return "investigator complete"

    async def correction() -> str:
        attempts.append("correction")
        if attempts.count("correction") == 1:
            raise _provider_error(headers={"retry-after-ms": "20000"})
        return "correction complete"

    async def synthesis() -> str:
        attempts.append("synthesis")
        return "synthesis complete"

    async def halo_engine() -> None:
        attempts.append("halo-engine")
        raise _provider_error(headers={"retry-after": "1"})

    assert await policy.run(investigator) == "investigator complete"
    assert await policy.run(correction) == "correction complete"
    assert await policy.run(synthesis) == "synthesis complete"
    with pytest.raises(ProviderAvailabilityBlockedError) as raised:
        await policy.run(halo_engine)
    assert waits == [10, 20]
    assert attempts == [
        "investigator",
        "investigator",
        "correction",
        "correction",
        "synthesis",
        "halo-engine",
    ]
    assert raised.value.receipt.retries_used == 2
    assert raised.value.receipt.waited_seconds == 30


async def test_provider_policy_blocks_long_cooldown_and_suppresses_later_operations() -> None:
    policy = ProviderAvailabilityPolicy(sleep=lambda _seconds: _no_wait())
    calls = 0

    async def halo_engine() -> None:
        nonlocal calls
        calls += 1
        raise _provider_error(headers={"retry-after": "31"})

    async def final_synthesis() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(ProviderAvailabilityBlockedError) as raised:
        await policy.run(halo_engine)
    with pytest.raises(ProviderAvailabilityBlockedError):
        await policy.run(final_synthesis)

    receipt = raised.value.receipt
    assert calls == 1
    assert receipt.classification == ProviderBlockerClassification.COOLDOWN_EXCEEDS_BUDGET
    assert receipt.retries_used == 0
    assert receipt.waited_seconds == 0


async def test_provider_policy_only_promotes_explicit_quota_codes() -> None:
    policy = ProviderAvailabilityPolicy()

    async def quota() -> None:
        raise _provider_error(
            body={"error": {"code": "insufficient_quota", "message": "secret body"}}
        )

    with pytest.raises(ProviderAvailabilityBlockedError) as raised:
        await policy.run(quota)

    persisted = raised.value.receipt.model_dump_json()
    assert raised.value.receipt.classification == ProviderBlockerClassification.QUOTA_EXHAUSTED
    assert "secret body" not in persisted
    assert "provider.invalid" not in persisted


@pytest.mark.parametrize(
    "error",
    [
        _provider_error(),
        _provider_error(status_code=500),
        ConnectionError("network unavailable"),
    ],
)
async def test_provider_policy_leaves_ambiguous_and_network_errors_unclassified(
    error: Exception,
) -> None:
    policy = ProviderAvailabilityPolicy()

    async def operation() -> None:
        raise error

    with pytest.raises(type(error)):
        await policy.run(operation)
    assert policy.receipt is None


async def _no_wait() -> None:
    return None
