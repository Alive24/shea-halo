from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn, TypeAlias

from agents import OpenAIProvider, RunConfig, set_default_openai_api
from openai import APIStatusError

ApiMode: TypeAlias = Literal["responses", "chat_completions"]
SUPPORTED_API_MODES: tuple[ApiMode, ...] = ("responses", "chat_completions")
_UNSUPPORTED_API_MODE_STATUS_CODES = frozenset({404, 405, 501})


class ProviderApiModeError(RuntimeError):
    """A provider rejected the configured OpenAI-compatible API route."""

    def __init__(self, api_mode: ApiMode, status_code: int) -> None:
        self.api_mode = api_mode
        self.status_code = status_code
        super().__init__(
            f"provider rejected configured API mode {api_mode!r} with HTTP {status_code}"
        )


@dataclass(frozen=True)
class OpenAIModelBootstrap:
    """Resolve one validated API mode across every Agents SDK model call."""

    api_mode: ApiMode

    @property
    def use_responses(self) -> bool:
        return self.api_mode == "responses"

    def provider(self) -> OpenAIProvider:
        return OpenAIProvider(use_responses=self.use_responses)

    def run_config(self, *, tracing_disabled: bool = True) -> RunConfig:
        return RunConfig(
            model_provider=self.provider(),
            tracing_disabled=tracing_disabled,
        )

    def configure_sdk_default(self) -> None:
        set_default_openai_api(self.api_mode)

    def reraise_if_unsupported(self, error: Exception) -> None:
        if (
            isinstance(error, APIStatusError)
            and error.status_code in _UNSUPPORTED_API_MODE_STATUS_CODES
        ):
            self._raise_unsupported(error.status_code, error)

    def _raise_unsupported(self, status_code: int, error: Exception) -> NoReturn:
        raise ProviderApiModeError(self.api_mode, status_code) from error
