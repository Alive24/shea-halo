from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"
HOST_PATH_REDACTED = "[HOST_PATH]"

_SENSITIVE_NAME_PARTS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
)
_COMMON_SENSITIVE_NAMES = {
    "AWS_PROFILE",
    "DATABASE_URL",
    "DOCKER_CONFIG",
    "GIT_ASKPASS",
    "KUBECONFIG",
    "NETRC",
    "NPM_CONFIG_USERCONFIG",
    "PIP_CONFIG_FILE",
    "REDIS_URL",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
}
_SAFE_GIT_AUTH_HANDLES = {"GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK"}
_SAFE_NUMERIC_TELEMETRY_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
_TELEMETRY_ATTRIBUTE_KEY = re.compile(r"^\$?[A-Za-z][A-Za-z0-9_.]{0,127}$")
_SENSITIVE_PATH_NAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
_SENSITIVE_PATH_SUFFIXES = {".key", ".p12", ".pem"}
_SOURCE_OR_DOCUMENT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".md",
    ".py",
    ".pyi",
    ".rb",
    ".rs",
    ".rst",
    ".swift",
    ".ts",
    ".tsx",
}
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?im)(?P<prefix>^\s*(?:authorization|proxy-authorization)\s*:\s*)"
    r"(?P<value>[^\r\n]+)"
)
_COOKIE_HEADER = re.compile(
    r"(?im)(?P<prefix>^\s*(?:cookie|set-cookie)\s*:\s*)"
    r"(?P<value>[^\r\n=]+=[^\r\n]+)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----"
    r".*?"
    r"-----END(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.DOTALL,
)
_CREDENTIAL_URL = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)"
    r"(?P<userinfo>[^/@\s:]+:[^/@\s]+)@",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_NAME = (
    r"[A-Za-z0-9_.-]{0,64}"
    r"(?:key|token|secret|password|credential|auth)"
    r"[A-Za-z0-9_.-]{0,64}"
)
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?P<prefix>[\"']?{_SENSITIVE_ASSIGNMENT_NAME}[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]*)(?P=quote)",
    re.IGNORECASE,
)
_UNQUOTED_ASSIGNMENT = re.compile(
    rf"(?P<prefix>\b{_SENSITIVE_ASSIGNMENT_NAME}\b[ \t]*[:=][ \t]*)"
    r"(?P<value>(?![\"'])[^\s,;]+)",
    re.IGNORECASE,
)
_PLACEHOLDER_VALUES = {
    "",
    "changeme",
    "example",
    "none",
    "null",
    "redacted",
    "test",
    "undefined",
}
_SHEBANG_PATH = re.compile(r"(?m)(?P<prefix>^#!)(?P<path>/[^\s]+)")
_SAFE_SHEBANG_PATHS = {
    "/bin/bash",
    "/bin/sh",
    "/bin/zsh",
    "/usr/bin/env",
    "/usr/bin/node",
    "/usr/bin/perl",
    "/usr/bin/python",
    "/usr/bin/python3",
    "/usr/bin/ruby",
}
_POSIX_HOST_PATH = re.compile(
    r"(?<!#!)(?<![A-Za-z0-9+.-]:)(?<![A-Za-z0-9_./+=>\]-])"
    r"(?:"
    r"/(?:Users|home|Volumes|private|tmp|var|opt|etc|usr|Library|root|mnt|srv|"
    r"workspace|Applications|System|nix|data|code|app|project|builds)"
    r"(?:/[^\s\\=\"'<>`|,;)\]}+]*)?"
    r"|/(?:[^\s/\\=\"'<>`|,;)\]}+]+/){2,}[^\s/\\=\"'<>`|,;)\]}+]*"
    r")"
)
_WINDOWS_HOST_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/]"
    r"(?:[^\s\"'<>`|,;)\]}]+[\\/]?)*"
)
_FILE_URI = re.compile(r"(?i)\bfile://(?:localhost)?/{1,3}[^\s\"'<>`|,;)\]}]+")
_UNC_HOST_PATH = re.compile(
    r"(?<![\\A-Za-z0-9_.-])\\\\[^\\\s\"'<>`|,;)\]}]+"
    r"\\[^\\\s\"'<>`|,;)\]}]+"
    r"(?:\\[^\\\s\"'<>`|,;)\]}]+)*"
)
_RUNTIME_ENDPOINT = re.compile(
    r"(?i)\b(?:grpc|grpcs|http|https|otlp)://[^\s\"'<>`|)\]}]+"
    r"|(?<![A-Za-z0-9_.-])(?:127(?:\.[0-9]{1,3}){3}|localhost|\[?::1\]?):[0-9]{2,5}"
    r"(?:/[^\s\"'<>`|)\]}]*)?"
)


def _is_sensitive_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _COMMON_SENSITIVE_NAMES or any(
        marker in normalized for marker in _SENSITIVE_NAME_PARTS
    )


def _is_safe_numeric_telemetry(name: str, value: Any) -> bool:
    """Keep explicit aggregate counts without weakening credential-name redaction."""

    if name.casefold() not in _SAFE_NUMERIC_TELEMETRY_FIELDS:
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 0
    if not isinstance(value, Mapping) or set(value) != {"value", "sources"}:
        return False
    count = value.get("value")
    sources = value.get("sources")
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
        and isinstance(sources, (list, tuple))
        and 0 < len(sources) <= 8
        and all(
            isinstance(source, Mapping)
            and set(source) == {"trace_id", "span_id", "source_key"}
            and all(isinstance(item, str) for item in source.values())
            for source in sources
        )
    )


def _is_safe_telemetry_provenance(name: str, value: Any) -> bool:
    """Allow an attribute name as provenance, never an arbitrary attribute value."""

    return (
        name.casefold() == "source_key"
        and isinstance(value, str)
        and bool(_TELEMETRY_ATTRIBUTE_KEY.fullmatch(value))
        and not contains_sensitive_text(value, {})
    )


def _source_environment(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _known_sensitive_values(environment: Mapping[str, str] | None) -> list[str]:
    source = _source_environment(environment)
    values = {
        value
        for name, value in source.items()
        if _is_sensitive_name(name) and isinstance(value, str) and value
    }
    return sorted(values, key=len, reverse=True)


def sanitized_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment without credentials or credential-provider handles."""
    source = _source_environment(environment)
    return {name: value for name, value in source.items() if not _is_sensitive_name(name)}


def git_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a constrained Git environment while preserving authentication handles."""
    source = _source_environment(environment)
    sanitized = {
        name: value
        for name, value in sanitized_environment(source).items()
        if not name.startswith("GIT_") or name in _SAFE_GIT_AUTH_HANDLES
    }
    sanitized.update(
        {
            name: source[name]
            for name in _SAFE_GIT_AUTH_HANDLES
            if isinstance(source.get(name), str) and source[name]
        }
    )
    sanitized["GIT_NO_REPLACE_OBJECTS"] = "1"
    return sanitized


def is_sensitive_path(path: Path) -> bool:
    """Return whether a path conventionally stores credentials or private keys."""
    for part in path.parts:
        name = part.casefold()
        candidate = Path(name)
        if (
            name.startswith(".env")
            or name in _SENSITIVE_PATH_NAMES
            or candidate.suffix in _SENSITIVE_PATH_SUFFIXES
        ):
            return True
    basename = path.name.casefold()
    return ("credential" in basename or "secret" in basename) and Path(
        basename
    ).suffix not in _SOURCE_OR_DOCUMENT_SUFFIXES


def redact_text(text: str, environment: Mapping[str, str] | None = None) -> str:
    """Remove known environment secrets and common credential forms from text."""
    redacted = text
    for value in _known_sensitive_values(environment):
        redacted = redacted.replace(value, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, redacted)
    redacted = _AUTHORIZATION_HEADER.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _COOKIE_HEADER.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )
    redacted = _CREDENTIAL_URL.sub(
        lambda match: f"{match.group('scheme')}{REDACTED}@",
        redacted,
    )
    lowered = redacted.casefold()
    if not any(
        marker in lowered for marker in ("key", "token", "secret", "password", "credential", "auth")
    ):
        return redacted
    redacted = _QUOTED_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{REDACTED}{match.group('quote')}"
        ),
        redacted,
    )
    return _UNQUOTED_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        redacted,
    )


def redact_host_paths(
    text: str,
    path_labels: Mapping[Path, str] | None = None,
) -> str:
    """Replace host paths with caller-provided logical roots or a generic marker.

    Explicit roots are replaced longest-first so a managed worktree nested below
    the target checkout is labelled as ``<workspace>`` rather than ``<target>``.
    Remaining common POSIX and Windows host paths are removed without treating
    URL paths or repository-relative paths as host filesystem locations.
    """

    redacted = _SHEBANG_PATH.sub(
        lambda match: (
            match.group(0)
            if match.group("path") in _SAFE_SHEBANG_PATHS
            else f"{match.group('prefix')}{HOST_PATH_REDACTED}"
        ),
        text,
    )
    labels = path_labels or {}
    replacements: list[tuple[str, str]] = []
    for raw_path, label in labels.items():
        rendered = os.fspath(raw_path)
        if not rendered or rendered in {"/", "\\"}:
            continue
        replacements.append((rendered.rstrip("/\\"), label))
    for rendered, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        variants = {rendered, rendered.replace("\\", "/")}
        for variant in variants:
            pattern = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(variant)}(?![A-Za-z0-9_.-])")
            redacted = pattern.sub(lambda _match: label, redacted)
    redacted = redacted.replace("file://<", "<")
    redacted = _FILE_URI.sub(HOST_PATH_REDACTED, redacted)
    redacted = _UNC_HOST_PATH.sub(HOST_PATH_REDACTED, redacted)
    redacted = _WINDOWS_HOST_PATH.sub(HOST_PATH_REDACTED, redacted)
    return _POSIX_HOST_PATH.sub(HOST_PATH_REDACTED, redacted)


def contains_host_absolute_path(text: str) -> bool:
    """Return whether text still contains a recognizable host absolute path."""

    shebangs = tuple(_SHEBANG_PATH.finditer(text))
    if any(match.group("path") not in _SAFE_SHEBANG_PATHS for match in shebangs):
        return True
    without_safe_shebangs = _SHEBANG_PATH.sub("#!<SYSTEM_INTERPRETER>", text)
    return bool(
        _FILE_URI.search(without_safe_shebangs)
        or _UNC_HOST_PATH.search(without_safe_shebangs)
        or _POSIX_HOST_PATH.search(without_safe_shebangs)
        or _WINDOWS_HOST_PATH.search(without_safe_shebangs)
    )


class PersistenceSafetyError(ValueError):
    """Raised when content cannot be made safe for durable storage."""


def sanitize_persisted_text(
    text: str,
    *,
    path_labels: Mapping[Path, str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Deterministically sanitize text before it crosses a persistence boundary."""

    sanitized = redact_text(text, environment)
    sanitized = redact_host_paths(sanitized, path_labels)
    if contains_sensitive_text(sanitized, environment):
        raise PersistenceSafetyError("persisted text still contains credential material")
    if contains_host_absolute_path(sanitized):
        raise PersistenceSafetyError("persisted text still contains a host absolute path")
    return sanitized


def sanitize_visible_text(
    text: str,
    *,
    path_labels: Mapping[Path, str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Sanitize a public summary and remove runtime endpoint values."""

    sanitized = sanitize_persisted_text(
        text,
        path_labels=path_labels,
        environment=environment,
    )
    return _RUNTIME_ENDPOINT.sub("[ENDPOINT]", sanitized)


def redact_data(value: Any, environment: Mapping[str, str] | None = None) -> Any:
    """Recursively redact structured data without corrupting its serialization."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            redacted[key] = (
                REDACTED
                if _is_sensitive_name(key)
                and not _is_safe_numeric_telemetry(key, item)
                and not _is_safe_telemetry_provenance(key, item)
                else redact_data(item, environment)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_data(item, environment) for item in value]
    if isinstance(value, str):
        return redact_text(value, environment)
    return value


def sanitize_persisted_data(
    value: Any,
    *,
    path_labels: Mapping[Path, str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> Any:
    """Recursively sanitize structured values before durable serialization."""

    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = sanitize_persisted_text(
                str(raw_key),
                path_labels=path_labels,
                environment=environment,
            )
            if key in sanitized:
                raise PersistenceSafetyError(
                    "structured persistence redaction produced duplicate keys"
                )
            sanitized[key] = (
                REDACTED
                if _is_sensitive_name(key)
                and not _is_safe_numeric_telemetry(key, item)
                and not _is_safe_telemetry_provenance(key, item)
                else sanitize_persisted_data(
                    item,
                    path_labels=path_labels,
                    environment=environment,
                )
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            sanitize_persisted_data(
                item,
                path_labels=path_labels,
                environment=environment,
            )
            for item in value
        ]
    if isinstance(value, str):
        return sanitize_persisted_text(
            value,
            path_labels=path_labels,
            environment=environment,
        )
    return value


def _looks_like_literal_secret(value: str, *, quoted: bool) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    if len(normalized) < 8 or normalized in _PLACEHOLDER_VALUES:
        return False
    if any(
        marker in normalized
        for marker in (
            "[redacted]",
            "${",
            "getenv(",
            "os.environ",
            "process.env",
            "your-",
            "your_",
        )
    ):
        return False
    if not quoted:
        if any(character in normalized for character in "()[]{}"):
            return False
        if re.fullmatch(r"[a-z_][a-z0-9_.]*", normalized):
            return False
        if normalized.isalpha() and len(normalized) < 20:
            return False
    return True


def contains_sensitive_text(text: str, environment: Mapping[str, str] | None = None) -> bool:
    """Return whether text contains a known secret or a likely literal credential."""
    if any(value in text for value in _known_sensitive_values(environment)):
        return True
    if any(pattern.search(text) for pattern in _TOKEN_PATTERNS):
        return True
    if any(
        match.group("value").strip() != REDACTED
        for pattern in (_AUTHORIZATION_HEADER, _COOKIE_HEADER)
        for match in pattern.finditer(text)
    ):
        return True
    if _PRIVATE_KEY_BLOCK.search(text) or _CREDENTIAL_URL.search(text):
        return True
    lowered = text.casefold()
    if not any(
        marker in lowered for marker in ("key", "token", "secret", "password", "credential", "auth")
    ):
        return False
    return any(
        _looks_like_literal_secret(match.group("value"), quoted=True)
        for match in _QUOTED_ASSIGNMENT.finditer(text)
    ) or any(
        _looks_like_literal_secret(match.group("value"), quoted=False)
        for match in _UNQUOTED_ASSIGNMENT.finditer(text)
    )
