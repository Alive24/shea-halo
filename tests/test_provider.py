from __future__ import annotations

from typing import TypeGuard

import httpx
import pytest
from agents import OpenAIChatCompletionsModel, OpenAIResponsesModel
from openai import APIStatusError

from shea_halo.provider import ApiMode, OpenAIModelBootstrap, ProviderApiModeError


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
