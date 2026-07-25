from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import ssl
import tomllib
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse, urlsplit

from shea_halo.config import validate_catalyst_sdk_base_endpoint
from shea_halo.models import (
    ExternalBlocker,
    HaloDesktopPreflightClassification,
    HaloDesktopPreflightReceipt,
    HaloDesktopPreflightResult,
    HttpStatusClass,
)
from shea_halo.runtime_fs import RuntimeFilesystem, RuntimeFilesystemError

_LINK = re.compile(r"^- \[([^\]]+)\]\((https://[^)]+)\)(?::\s*(.*))?$", re.MULTILINE)
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_WORD = re.compile(r"[a-z0-9]+")
_GENERIC = {
    "agent",
    "agents",
    "ai",
    "api",
    "client",
    "core",
    "framework",
    "integration",
    "sdk",
    "trace",
    "traces",
}
_SKIP_DIRECTORIES = {
    ".git",
    ".shea",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
INFERENCE_TRACE_CATALOG_URL = "https://docs.inference.net/llms.txt"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
HALO_DESKTOP_SERVICE = "halo-canvas-telemetry"
_HEALTH_PATH = "/health"
_INGEST_PATH = "/v1/traces"
# Stable identifiers and timestamps make a repeated bounded worker attempt the
# same synthetic acceptance probe from Shea Halo's perspective.
_PROBE_PAYLOAD = json.dumps(
    {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "shea-halo-preflight"},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "shea-halo-preflight", "version": "1"},
                        "spans": [
                            {
                                "traceId": "8f5b8f5b8f5b8f5b8f5b8f5b8f5b8f5b",
                                "spanId": "4a6c4a6c4a6c4a6c",
                                "name": "shea-halo.external-desktop-preflight",
                                "kind": 1,
                                "startTimeUnixNano": "1735689600000000000",
                                "endTimeUnixNano": "1735689600001000000",
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    },
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")


class IntegrationError(RuntimeError):
    pass


class _ResponseTooLarge(IntegrationError):
    pass


class _InvalidHttpResponse(IntegrationError):
    pass


@dataclass(frozen=True)
class HaloDesktopPreflightLimits:
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 2.0
    total_timeout_seconds: float = 5.0
    max_request_bytes: int = 16 * 1024
    max_response_bytes: int = 16 * 1024
    max_header_bytes: int = 16 * 1024

    def __post_init__(self) -> None:
        durations = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.total_timeout_seconds,
        )
        sizes = (
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_header_bytes,
        )
        if any(value <= 0 for value in durations) or any(value <= 0 for value in sizes):
            raise ValueError("Desktop preflight limits must be positive")


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _desktop_surface_url(base_endpoint: str, suffix: str) -> str:
    parsed = urlsplit(base_endpoint)
    base_path = parsed.path.rstrip("/")
    return parsed._replace(path=f"{base_path}{suffix}").geturl()


def _status_class(status: int) -> HttpStatusClass:
    if status < 100 or status > 599:
        raise _InvalidHttpResponse("invalid HTTP status")
    return cast(HttpStatusClass, f"{status // 100}xx")


async def _readline(
    reader: asyncio.StreamReader,
    *,
    timeout: float,
    maximum: int,
) -> bytes:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise _InvalidHttpResponse("invalid HTTP response line") from exc
    if not line or len(line) > maximum:
        raise _InvalidHttpResponse("invalid HTTP response line")
    return line


async def _read_exactly(
    reader: asyncio.StreamReader,
    size: int,
    *,
    timeout: float,
) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    except asyncio.IncompleteReadError as exc:
        raise _InvalidHttpResponse("truncated HTTP response") from exc


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    *,
    limits: HaloDesktopPreflightLimits,
) -> bytes:
    body = bytearray()
    trailer_bytes = 0
    while True:
        line = await _readline(
            reader,
            timeout=limits.read_timeout_seconds,
            maximum=256,
        )
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise _InvalidHttpResponse("invalid chunked HTTP response") from exc
        if size < 0 or len(body) + size > limits.max_response_bytes:
            raise _ResponseTooLarge("HTTP response body exceeded its bound")
        if size == 0:
            while True:
                trailer = await _readline(
                    reader,
                    timeout=limits.read_timeout_seconds,
                    maximum=limits.max_header_bytes,
                )
                trailer_bytes += len(trailer)
                if trailer_bytes > limits.max_header_bytes:
                    raise _InvalidHttpResponse("HTTP trailers exceeded their bound")
                if trailer in {b"\r\n", b"\n"}:
                    return bytes(body)
        body.extend(
            await _read_exactly(
                reader,
                size,
                timeout=limits.read_timeout_seconds,
            )
        )
        if (
            await _read_exactly(
                reader,
                2,
                timeout=limits.read_timeout_seconds,
            )
            != b"\r\n"
        ):
            raise _InvalidHttpResponse("invalid chunked HTTP response")


async def _read_response_body(
    reader: asyncio.StreamReader,
    *,
    status: int,
    headers: dict[str, str],
    limits: HaloDesktopPreflightLimits,
) -> bytes:
    if status in {204, 304} or 100 <= status < 200:
        return b""
    if "chunked" in headers.get("transfer-encoding", "").casefold():
        return await _read_chunked_body(reader, limits=limits)
    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError as exc:
            raise _InvalidHttpResponse("invalid HTTP content length") from exc
        if size < 0:
            raise _InvalidHttpResponse("invalid HTTP content length")
        if size > limits.max_response_bytes:
            raise _ResponseTooLarge("HTTP response body exceeded its bound")
        return await _read_exactly(
            reader,
            size,
            timeout=limits.read_timeout_seconds,
        )

    body = bytearray()
    while True:
        remaining = limits.max_response_bytes + 1 - len(body)
        chunk = await asyncio.wait_for(
            reader.read(min(8 * 1024, remaining)),
            timeout=limits.read_timeout_seconds,
        )
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > limits.max_response_bytes:
            raise _ResponseTooLarge("HTTP response body exceeded its bound")
    return bytes(body)


async def _http_request(
    url: str,
    *,
    method: str,
    body: bytes,
    authorization_token: str | None,
    limits: HaloDesktopPreflightLimits,
) -> _HttpResponse:
    if len(body) > limits.max_request_bytes:
        raise IntegrationError("Desktop preflight request exceeded its bound")
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise IntegrationError("Desktop preflight endpoint is invalid")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            host,
            port,
            ssl=ssl_context,
            server_hostname=host if ssl_context is not None else None,
            limit=limits.max_header_bytes + 1,
        ),
        timeout=limits.connect_timeout_seconds,
    )
    try:
        target = parsed.path or "/"
        headers = [
            f"{method} {target} HTTP/1.1",
            f"Host: {parsed.netloc}",
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: close",
            "User-Agent: shea-halo/0.1",
        ]
        if body:
            headers.extend(
                [
                    "Content-Type: application/json",
                    f"Content-Length: {len(body)}",
                ]
            )
        if authorization_token:
            if "\r" in authorization_token or "\n" in authorization_token:
                raise IntegrationError("Desktop preflight credential is invalid")
            headers.append(f"Authorization: Bearer {authorization_token}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        if len(request) > limits.max_request_bytes:
            raise IntegrationError("Desktop preflight request exceeded its bound")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=limits.read_timeout_seconds)

        header_bytes = 0
        status_line = await _readline(
            reader,
            timeout=limits.read_timeout_seconds,
            maximum=limits.max_header_bytes,
        )
        header_bytes += len(status_line)
        try:
            protocol, raw_status, _reason = status_line.decode("iso-8859-1").split(" ", 2)
            status = int(raw_status)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _InvalidHttpResponse("invalid HTTP status line") from exc
        if protocol not in {"HTTP/1.0", "HTTP/1.1"}:
            raise _InvalidHttpResponse("unsupported HTTP protocol")
        _status_class(status)

        response_headers: dict[str, str] = {}
        while True:
            line = await _readline(
                reader,
                timeout=limits.read_timeout_seconds,
                maximum=limits.max_header_bytes,
            )
            header_bytes += len(line)
            if header_bytes > limits.max_header_bytes:
                raise _InvalidHttpResponse("HTTP response headers exceeded their bound")
            if line in {b"\r\n", b"\n"}:
                break
            try:
                name, value = line.decode("iso-8859-1").split(":", 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise _InvalidHttpResponse("invalid HTTP response header") from exc
            normalized_name = name.strip().casefold()
            if not normalized_name:
                raise _InvalidHttpResponse("invalid HTTP response header")
            response_headers[normalized_name] = value.strip()

        response_body = await _read_response_body(
            reader,
            status=status,
            headers=response_headers,
            limits=limits,
        )
        return _HttpResponse(status=status, headers=response_headers, body=response_body)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _preflight_blocker(
    classification: HaloDesktopPreflightClassification,
) -> ExternalBlocker:
    descriptions = {
        HaloDesktopPreflightClassification.UNREACHABLE_SERVICE: (
            "The configured external HALO Desktop service is unreachable."
        ),
        HaloDesktopPreflightClassification.TIMEOUT: (
            "The configured external HALO Desktop preflight timed out."
        ),
        HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE: (
            "The configured external service did not expose a compatible HALO Desktop "
            "health response."
        ),
        HaloDesktopPreflightClassification.UNSUPPORTED_CONTENT_TYPE: (
            "The configured external HALO Desktop service did not accept OTLP/JSON."
        ),
        HaloDesktopPreflightClassification.REJECTED_INGEST: (
            "The configured external HALO Desktop service rejected the synthetic OTLP/JSON trace."
        ),
        HaloDesktopPreflightClassification.OVERSIZED_RESPONSE: (
            "The configured external HALO Desktop service returned an oversized response."
        ),
    }
    return ExternalBlocker(
        category=(
            "network"
            if classification
            in {
                HaloDesktopPreflightClassification.UNREACHABLE_SERVICE,
                HaloDesktopPreflightClassification.TIMEOUT,
            }
            else "service_unavailable"
        ),
        description=descriptions[classification],
        required_action=(
            "Start or repair the operator-managed HALO Desktop service, confirm its documented "
            "public health and OTLP/JSON surfaces, then return the item to Halo Research."
        ),
    )


async def preflight_halo_desktop(
    base_endpoint: str,
    *,
    authorization_token: str | None = None,
    limits: HaloDesktopPreflightLimits | None = None,
) -> HaloDesktopPreflightResult:
    """Probe only HALO Desktop's documented health and OTLP/JSON ingest surfaces."""

    active_limits = limits or HaloDesktopPreflightLimits()
    started_at = datetime.now(UTC)
    payload_sha256 = hashlib.sha256(_PROBE_PAYLOAD).hexdigest()
    health_status_class: HttpStatusClass | None = None
    ingest_status_class: HttpStatusClass | None = None
    service: str | None = None
    classification = HaloDesktopPreflightClassification.UNREACHABLE_SERVICE
    stage = "health"
    try:
        async with asyncio.timeout(active_limits.total_timeout_seconds):
            validated_endpoint = validate_catalyst_sdk_base_endpoint(base_endpoint)
            health = await _http_request(
                _desktop_surface_url(validated_endpoint, _HEALTH_PATH),
                method="GET",
                body=b"",
                authorization_token=None,
                limits=active_limits,
            )
            health_status_class = _status_class(health.status)
            if health_status_class != "2xx":
                classification = HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE
            elif "application/json" not in health.headers.get("content-type", "").casefold():
                classification = HaloDesktopPreflightClassification.UNSUPPORTED_CONTENT_TYPE
            else:
                try:
                    health_document = json.loads(health.body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    classification = HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE
                else:
                    if (
                        not isinstance(health_document, dict)
                        or health_document.get("ok") is not True
                        or health_document.get("service") != HALO_DESKTOP_SERVICE
                    ):
                        classification = (
                            HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE
                        )
                    else:
                        # Deliberately retain only the allowlisted identity; fields such as
                        # dbPath never cross this local parsing boundary.
                        service = HALO_DESKTOP_SERVICE
                        stage = "ingest"
                        ingest = await _http_request(
                            _desktop_surface_url(validated_endpoint, _INGEST_PATH),
                            method="POST",
                            body=_PROBE_PAYLOAD,
                            authorization_token=authorization_token,
                            limits=active_limits,
                        )
                        ingest_status_class = _status_class(ingest.status)
                        if ingest.status == 415:
                            classification = (
                                HaloDesktopPreflightClassification.UNSUPPORTED_CONTENT_TYPE
                            )
                        elif ingest_status_class != "2xx":
                            classification = HaloDesktopPreflightClassification.REJECTED_INGEST
                        else:
                            classification = HaloDesktopPreflightClassification.PASSED
    except TimeoutError:
        classification = HaloDesktopPreflightClassification.TIMEOUT
    except _ResponseTooLarge:
        classification = HaloDesktopPreflightClassification.OVERSIZED_RESPONSE
    except _InvalidHttpResponse:
        classification = (
            HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE
            if stage == "health"
            else HaloDesktopPreflightClassification.REJECTED_INGEST
        )
    except (IntegrationError, OSError, ssl.SSLError, UnicodeError, ValueError):
        classification = HaloDesktopPreflightClassification.UNREACHABLE_SERVICE

    receipt = HaloDesktopPreflightReceipt(
        service=service if classification == HaloDesktopPreflightClassification.PASSED else None,
        health_status_class=health_status_class,
        ingest_status_class=ingest_status_class,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        payload_sha256=payload_sha256,
        classification=classification,
    )
    passed = classification == HaloDesktopPreflightClassification.PASSED
    return HaloDesktopPreflightResult(
        passed=passed,
        receipt=receipt,
        blocker=None if passed else _preflight_blocker(classification),
    )


@dataclass(frozen=True)
class GuideRef:
    title: str
    url: str
    description: str
    score: int = 0
    evidence: tuple[str, ...] = ()
    stale: bool = False


@dataclass(frozen=True)
class FetchedGuide:
    ref: GuideRef
    content: str
    retrieved_at: datetime
    sha256: str
    cache_path: Path
    stale: bool = False


def _words(value: str) -> set[str]:
    return {word for word in _WORD.findall(value.lower()) if word not in _GENERIC}


def parse_trace_catalog(text: str, *, stale: bool = False) -> list[GuideRef]:
    guides: list[GuideRef] = []
    for title, url, description in _LINK.findall(text):
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "docs.inference.net"
            or "/integrations/traces/" not in parsed.path
            or not parsed.path.endswith(".md")
        ):
            continue
        guides.append(
            GuideRef(
                title=title,
                url=url,
                description=description,
                stale=stale,
            )
        )
    if not guides:
        raise IntegrationError("Inference catalog contained no tracing integration guides")
    return guides


def _dependency_name(requirement: str) -> str | None:
    match = _REQUIREMENT.match(requirement)
    return match.group(1).lower().replace("_", "-") if match else None


def _manifest_paths(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    manifests: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = [
            directory for directory in directories if directory not in _SKIP_DIRECTORIES
        ]
        current_path = Path(current)
        for name in files:
            if (
                name in {"package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
                or name.startswith("requirements")
                and name.endswith(".txt")
            ):
                manifest = current_path / name
                try:
                    resolved = manifest.resolve(strict=True)
                except OSError as exc:
                    raise IntegrationError(
                        f"failed to resolve dependency manifest: {manifest}"
                    ) from exc
                if manifest.is_symlink() or not resolved.is_relative_to(root):
                    raise IntegrationError(
                        f"dependency manifest escapes the target repository: {manifest}"
                    )
                if manifest.stat().st_size > MAX_MANIFEST_BYTES:
                    raise IntegrationError(
                        f"dependency manifest exceeds {MAX_MANIFEST_BYTES} bytes: {manifest}"
                    )
                manifests.append(manifest)
        if len(manifests) > 500:
            raise IntegrationError("target contains more than 500 dependency manifests")
    return sorted(manifests)


def discover_dependencies(root: Path) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for manifest in _manifest_paths(root):
        if manifest.name == "package.json":
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for section in (
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            ):
                values = data.get(section, {})
                if isinstance(values, dict):
                    dependencies.update(
                        {
                            str(name).lower(): str(version)
                            for name, version in values.items()
                            if isinstance(version, str)
                        }
                    )
        elif manifest.name == "pyproject.toml":
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            project = data.get("project", {})
            if isinstance(project, dict):
                requirements = project.get("dependencies", [])
                if isinstance(requirements, list):
                    for requirement in requirements:
                        if isinstance(requirement, str) and (name := _dependency_name(requirement)):
                            dependencies[name] = requirement
            poetry = data.get("tool", {})
            if isinstance(poetry, dict):
                poetry = poetry.get("poetry", {})
            if isinstance(poetry, dict):
                values = poetry.get("dependencies", {})
                if isinstance(values, dict):
                    dependencies.update(
                        {
                            str(name).lower(): str(version)
                            for name, version in values.items()
                            if name != "python"
                        }
                    )
        elif manifest.name.startswith("requirements"):
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith(("#", "-")):
                    if name := _dependency_name(line):
                        dependencies[name] = line.strip()
        elif manifest.name == "Cargo.toml":
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                values = data.get(section, {})
                if isinstance(values, dict):
                    dependencies.update(
                        {str(name).lower(): str(version) for name, version in values.items()}
                    )
        elif manifest.name == "go.mod":
            in_require_block = False
            for line in manifest.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                if stripped == "require (":
                    in_require_block = True
                    continue
                if stripped == ")" and in_require_block:
                    in_require_block = False
                    continue
                requirement = (
                    stripped.removeprefix("require ").strip()
                    if stripped.startswith("require ")
                    else stripped
                    if in_require_block
                    else ""
                )
                fields = requirement.split()
                if len(fields) >= 2:
                    dependencies[fields[0].lower()] = fields[1]

    return dict(sorted(dependencies.items()))


def rank_guides(guides: list[GuideRef], dependencies: dict[str, str]) -> list[GuideRef]:
    ranked: list[GuideRef] = []
    for guide in guides:
        guide_words = _words(f"{guide.title} {guide.url}")
        score = 0
        evidence: list[str] = []
        for name, version in dependencies.items():
            dependency_words = _words(name)
            overlap = dependency_words & guide_words
            normalized_name = "-".join(_WORD.findall(name.lower()))
            normalized_guide = "-".join(_WORD.findall(f"{guide.title} {guide.url}".lower()))
            dependency_score = len(overlap) * 3
            if len(normalized_name) >= 4 and normalized_name in normalized_guide:
                dependency_score += 8
            if dependency_score:
                score += dependency_score
                evidence.append(f"{name} {version}".strip())
        if score:
            ranked.append(
                GuideRef(
                    title=guide.title,
                    url=guide.url,
                    description=guide.description,
                    score=score,
                    evidence=tuple(sorted(evidence)),
                    stale=guide.stale,
                )
            )
    return sorted(ranked, key=lambda guide: (-guide.score, guide.title.lower()))


class IntegrationCatalog:
    def __init__(self, cache_dir: Path) -> None:
        """Use the one official machine-readable Inference documentation catalog."""
        candidate = str(cache_dir)
        if urlparse(candidate).scheme:
            raise IntegrationError(
                "IntegrationCatalog accepts a cache directory, not a catalog URL"
            )
        try:
            self.runtime = RuntimeFilesystem.from_managed_path(Path(cache_dir))
            self.cache_dir = self.runtime.path(Path(cache_dir))
        except RuntimeFilesystemError:
            raise IntegrationError(
                "IntegrationCatalog cache must stay below target/.shea"
            ) from None
        self.catalog_url = INFERENCE_TRACE_CATALOG_URL

    def _ensure_directory(self, path: Path) -> None:
        try:
            self.runtime.ensure_directory(path)
        except RuntimeFilesystemError:
            raise IntegrationError("integration cache directory is unsafe") from None

    def _read_cache(self, path: Path) -> str:
        try:
            return self.runtime.read_text(path)
        except FileNotFoundError:
            raise
        except RuntimeFilesystemError:
            raise IntegrationError("integration cache file is unsafe") from None

    def _write_cache(self, path: Path, content: str) -> None:
        try:
            self.runtime.atomic_write_text(path, content)
        except RuntimeFilesystemError:
            raise IntegrationError("integration cache file is unsafe") from None

    def _fetch(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "shea-halo/0.1 (+https://github.com/Alive24/shea-halo)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read(MAX_DOCUMENT_BYTES + 1)
        except OSError as exc:
            raise IntegrationError(f"failed to fetch {url}: {exc}") from exc
        if len(content) > MAX_DOCUMENT_BYTES:
            raise IntegrationError(f"official tracing document exceeded {MAX_DOCUMENT_BYTES} bytes")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrationError("official tracing document was not UTF-8") from exc

    def load(self) -> list[GuideRef]:
        self._ensure_directory(self.cache_dir)
        cache_path = self.cache_dir / "llms.txt"
        try:
            text = self._fetch(self.catalog_url)
        except IntegrationError as fetch_error:
            try:
                text = self._read_cache(cache_path)
            except FileNotFoundError:
                raise fetch_error
            guides = parse_trace_catalog(text, stale=True)
        else:
            guides = parse_trace_catalog(text)
            self._write_cache(cache_path, text)
        return guides

    def fetch_guide(self, ref: GuideRef) -> FetchedGuide:
        catalog_guides = self.load()
        catalog_ref = next((guide for guide in catalog_guides if guide.url == ref.url), None)
        if catalog_ref is None:
            raise IntegrationError("guide URL is not present in the current official catalog")
        catalog_stale = ref.stale or catalog_ref.stale
        guide_dir = self.cache_dir / "guides"
        self._ensure_directory(guide_dir)
        index_path = guide_dir / "index.json"
        index: dict[str, dict[str, str]] = {}
        try:
            index_text = self._read_cache(index_path)
        except FileNotFoundError:
            pass
        else:
            loaded = json.loads(index_text)
            if isinstance(loaded, dict):
                index = loaded
        stale = catalog_stale
        try:
            content = self._fetch(ref.url)
        except IntegrationError as fetch_error:
            cached = index.get(ref.url, {})
            digest = cached.get("sha256", "")
            retrieved_raw = cached.get("retrieved_at", "")
            cache_path = guide_dir / f"{digest}.md"
            if not digest or not retrieved_raw:
                raise fetch_error
            try:
                content = self._read_cache(cache_path)
            except FileNotFoundError:
                raise fetch_error
            retrieved_at = datetime.fromisoformat(retrieved_raw)
            stale = True
        else:
            digest = hashlib.sha256(content.encode()).hexdigest()
            retrieved_at = datetime.now(UTC)
            cache_path = guide_dir / f"{digest}.md"
            self._write_cache(cache_path, content)
            index[ref.url] = {
                "sha256": digest,
                "retrieved_at": retrieved_at.isoformat(),
            }
            self._write_cache(
                index_path,
                json.dumps(index, indent=2, sort_keys=True),
            )
        return FetchedGuide(
            ref=replace(ref, stale=stale),
            content=content,
            retrieved_at=retrieved_at,
            sha256=digest,
            cache_path=cache_path,
            stale=stale,
        )
