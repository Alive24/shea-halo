from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Literal, NoReturn, TypeAlias, TypeVar

from agents import OpenAIProvider, RunConfig, set_default_openai_api
from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

ApiMode: TypeAlias = Literal["responses", "chat_completions"]
SUPPORTED_API_MODES: tuple[ApiMode, ...] = ("responses", "chat_completions")
_UNSUPPORTED_API_MODE_STATUS_CODES = frozenset({404, 405, 501})
_EXPLICIT_QUOTA_CODES = frozenset(
    {"billing_hard_limit_reached", "insufficient_quota", "quota_exceeded", "usage_limit_exceeded"}
)
_MAX_PROVIDER_RETRIES = 2
_MAX_PROVIDER_WAIT_SECONDS = 30.0
_T = TypeVar("_T")


class ProviderBlockerClassification(StrEnum):
    """Sanitized terminal availability classifications for workpad persistence."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    COOLDOWN_EXCEEDS_BUDGET = "cooldown_exceeds_budget"


class ProviderAvailabilityReceipt(BaseModel):
    """Bounded provider-availability evidence; never contains provider payloads or errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compatibility: Literal["openai-compatible/v1"] = "openai-compatible/v1"
    classification: ProviderBlockerClassification
    retries_used: int = Field(ge=0, le=_MAX_PROVIDER_RETRIES)
    waited_seconds: float = Field(ge=0, le=_MAX_PROVIDER_WAIT_SECONDS)


class ProviderAvailabilityBlockedError(RuntimeError):
    """Stops later model operations after a verified provider-wide blocker."""

    def __init__(self, receipt: ProviderAvailabilityReceipt) -> None:
        self.receipt = receipt
        super().__init__(f"provider availability blocked: {receipt.classification.value}")


@dataclass(frozen=True)
class _ProviderSignal:
    classification: ProviderBlockerClassification | None = None
    retry_after_seconds: float | None = None


class ProviderAvailabilityPolicy:
    """Share a deliberately small provider retry budget across one research run."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._retries_used = 0
        self._waited_seconds = 0.0
        self._terminal: ProviderAvailabilityBlockedError | None = None

    @property
    def receipt(self) -> ProviderAvailabilityReceipt | None:
        return None if self._terminal is None else self._terminal.receipt

    async def run(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Run one model operation, retrying only an explicit short provider throttle."""

        if self._terminal is not None:
            raise self._terminal
        while True:
            try:
                return await operation()
            except ProviderAvailabilityBlockedError:
                raise
            except Exception as error:
                signal = _provider_signal(error)
                if signal.classification is not None:
                    raise self._block(signal.classification)
                delay = signal.retry_after_seconds
                if delay is None:
                    raise
                if (
                    self._retries_used >= _MAX_PROVIDER_RETRIES
                    or delay > _MAX_PROVIDER_WAIT_SECONDS - self._waited_seconds
                ):
                    raise self._block(ProviderBlockerClassification.COOLDOWN_EXCEEDS_BUDGET)
                self._retries_used += 1
                self._waited_seconds += delay
                await self._sleep(delay)

    def _block(
        self,
        classification: ProviderBlockerClassification,
    ) -> ProviderAvailabilityBlockedError:
        if self._terminal is None:
            self._terminal = ProviderAvailabilityBlockedError(
                ProviderAvailabilityReceipt(
                    classification=classification,
                    retries_used=self._retries_used,
                    waited_seconds=self._waited_seconds,
                )
            )
        return self._terminal


def _provider_signal(error: Exception) -> _ProviderSignal:
    """Read the versioned OpenAI-compatible compatibility boundary without persisting it."""

    if not isinstance(error, APIStatusError):
        return _ProviderSignal()
    if _explicit_quota_code(error):
        return _ProviderSignal(classification=ProviderBlockerClassification.QUOTA_EXHAUSTED)
    retry_after = _retry_after_seconds(error)
    if error.status_code == 429 and retry_after is not None:
        return _ProviderSignal(retry_after_seconds=retry_after)
    if retry_after is not None:
        return _ProviderSignal(retry_after_seconds=retry_after)
    return _ProviderSignal()


def _explicit_quota_code(error: APIStatusError) -> bool:
    """Recognize only stable OpenAI-compatible v1 quota codes, never message text."""

    values: list[object] = [getattr(error, "code", None)]
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        values.extend((body.get("code"), body.get("error")))
        nested = body.get("error")
        if isinstance(nested, Mapping):
            values.append(nested.get("code"))
    return any(
        isinstance(value, str) and value.casefold() in _EXPLICIT_QUOTA_CODES for value in values
    )


def _retry_after_seconds(error: APIStatusError) -> float | None:
    response = error.response
    value = response.headers.get("retry-after") or response.headers.get("retry-after-ms")
    if value is None:
        return None
    try:
        delay = float(value)
        if response.headers.get("retry-after") is None:
            delay /= 1_000
        return delay if delay >= 0 else None
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
            ).total_seconds()
        except (TypeError, ValueError):
            return None
        return delay if delay >= 0 else None


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
        # The OpenAI client retries throttles and network errors by default. Halo
        # owns retries at the research-run boundary, so every Agents SDK call must
        # expose its first provider response to ProviderAvailabilityPolicy.
        return OpenAIProvider(
            openai_client=AsyncOpenAI(max_retries=0),
            use_responses=self.use_responses,
        )

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
