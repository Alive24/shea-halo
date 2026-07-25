from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from shea_halo.integrations import (
    HALO_DESKTOP_SERVICE,
    INFERENCE_TRACE_CATALOG_URL,
    MAX_DOCUMENT_BYTES,
    HaloDesktopPreflightLimits,
    IntegrationCatalog,
    IntegrationError,
    discover_dependencies,
    parse_trace_catalog,
    preflight_halo_desktop,
    rank_guides,
)
from shea_halo.models import HaloDesktopPreflightClassification

CATALOG = """\
# Docs
- [Vercel Eve Traces](https://docs.inference.net/integrations/traces/eve.md): Eve
- [OpenAI Agents Traces](https://docs.inference.net/integrations/traces/openai-agents.md): Agents
- [Manual Spans](https://docs.inference.net/integrations/traces/manual-spans.md): Manual
- [Unrelated](https://example.com/integrations/traces/eve.md): no
"""

_SENTINEL_PATH = "/Users/alice/Library/Application Support/net.inference.halo/halo.sqlite"
_SENTINEL_CREDENTIAL = "Bearer secret-token-from-ingest-error"


@contextmanager
def desktop_server(
    routes: Mapping[str, tuple[int, str, bytes, float | int]],
    requests: list[dict[str, object]] | None = None,
) -> Iterator[str]:
    captured = requests if requests is not None else []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            captured.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "content_type": self.headers.get("content-type"),
                    "body": body,
                }
            )
            status, content_type, response_body, delay = routes.get(
                self.path,
                (404, "application/json", b"{}", 0),
            )
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                if 300 <= status < 400:
                    self.send_header("Location", "/private-surface")
                self.end_headers()
                self.wfile.write(response_body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            del format, args
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def healthy_response(*, service: str = HALO_DESKTOP_SERVICE) -> bytes:
    return json.dumps(
        {
            "ok": True,
            "service": service,
            "dbPath": _SENTINEL_PATH,
        }
    ).encode()


def small_preflight_limits(**updates: object) -> HaloDesktopPreflightLimits:
    values: dict[str, object] = {
        "connect_timeout_seconds": 0.2,
        "read_timeout_seconds": 0.2,
        "total_timeout_seconds": 0.5,
        "max_request_bytes": 4 * 1024,
        "max_response_bytes": 2 * 1024,
        "max_header_bytes": 2 * 1024,
    }
    values.update(updates)
    return HaloDesktopPreflightLimits(**values)  # type: ignore[arg-type]


async def test_external_desktop_preflight_uses_only_derived_public_paths() -> None:
    requests: list[dict[str, object]] = []
    routes = {
        "/collector/health": (200, "application/json", healthy_response(), 0),
        "/collector/v1/traces": (200, "application/json", b"{}", 0),
    }
    with desktop_server(routes, requests) as endpoint:
        result = await preflight_halo_desktop(
            f"{endpoint}/collector/v1/traces",
            authorization_token="credential-not-for-persistence",
            limits=small_preflight_limits(),
        )

    assert result.passed
    assert result.receipt.classification == HaloDesktopPreflightClassification.PASSED
    assert result.receipt.service == HALO_DESKTOP_SERVICE
    assert result.receipt.health_status_class == "2xx"
    assert result.receipt.ingest_status_class == "2xx"
    assert [(item["method"], item["path"]) for item in requests] == [
        ("GET", "/collector/health"),
        ("POST", "/collector/v1/traces"),
    ]
    payload = cast_bytes(requests[1]["body"])
    assert hashlib.sha256(payload).hexdigest() == result.receipt.payload_sha256
    rendered_payload = payload.decode()
    assert "shea-halo.external-desktop-preflight" in rendered_payload
    assert not any(
        forbidden in rendered_payload
        for forbidden in (
            "prompt",
            "output",
            "tool",
            "credential",
            "endpoint",
            _SENTINEL_PATH,
        )
    )
    persisted = result.model_dump_json()
    assert endpoint not in persisted
    assert _SENTINEL_PATH not in persisted
    assert "credential-not-for-persistence" not in persisted


def cast_bytes(value: object) -> bytes:
    assert isinstance(value, bytes)
    return value


async def test_external_desktop_preflight_classifies_unreachable_service() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = await preflight_halo_desktop(
        f"http://127.0.0.1:{port}",
        limits=small_preflight_limits(),
    )

    assert result.receipt.classification == (HaloDesktopPreflightClassification.UNREACHABLE_SERVICE)
    assert result.blocker is not None
    assert str(port) not in result.model_dump_json()


async def test_external_desktop_preflight_classifies_timeout() -> None:
    routes = {
        "/health": (200, "application/json", healthy_response(), 0.2),
    }
    with desktop_server(routes) as endpoint:
        result = await preflight_halo_desktop(
            endpoint,
            limits=small_preflight_limits(
                read_timeout_seconds=0.03,
                total_timeout_seconds=0.05,
            ),
        )

    assert result.receipt.classification == HaloDesktopPreflightClassification.TIMEOUT


@pytest.mark.parametrize(
    ("status", "content_type", "body", "classification"),
    [
        (
            302,
            "application/json",
            b"{}",
            HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE,
        ),
        (
            200,
            "text/html",
            b"<html></html>",
            HaloDesktopPreflightClassification.UNSUPPORTED_CONTENT_TYPE,
        ),
        (
            200,
            "application/json",
            b"{malformed",
            HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE,
        ),
        (
            200,
            "application/json",
            healthy_response(service="another-service"),
            HaloDesktopPreflightClassification.INCOMPATIBLE_HEALTH_RESPONSE,
        ),
    ],
)
async def test_external_desktop_preflight_fails_closed_on_incompatible_health(
    status: int,
    content_type: str,
    body: bytes,
    classification: HaloDesktopPreflightClassification,
) -> None:
    routes = {"/health": (status, content_type, body, 0)}
    requests: list[dict[str, object]] = []
    with desktop_server(routes, requests) as endpoint:
        result = await preflight_halo_desktop(
            endpoint,
            limits=small_preflight_limits(),
        )

    assert result.receipt.classification == classification
    assert result.receipt.ingest_status_class is None
    assert [item["path"] for item in requests] == ["/health"]
    assert _SENTINEL_PATH not in result.model_dump_json()
    assert "another-service" not in result.model_dump_json()


async def test_external_desktop_preflight_rejects_oversized_response() -> None:
    routes = {
        "/health": (200, "application/json", healthy_response() + b"x" * 512, 0),
    }
    with desktop_server(routes) as endpoint:
        result = await preflight_halo_desktop(
            endpoint,
            limits=small_preflight_limits(max_response_bytes=64),
        )

    assert result.receipt.classification == (HaloDesktopPreflightClassification.OVERSIZED_RESPONSE)
    assert _SENTINEL_PATH not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (400, HaloDesktopPreflightClassification.REJECTED_INGEST),
        (415, HaloDesktopPreflightClassification.UNSUPPORTED_CONTENT_TYPE),
    ],
)
async def test_external_desktop_preflight_sanitizes_ingest_failures(
    status: int,
    classification: HaloDesktopPreflightClassification,
) -> None:
    routes = {
        "/health": (200, "application/json", healthy_response(), 0),
        "/v1/traces": (
            status,
            "application/json",
            json.dumps({"error": _SENTINEL_CREDENTIAL}).encode(),
            0,
        ),
    }
    with desktop_server(routes) as endpoint:
        result = await preflight_halo_desktop(
            endpoint,
            limits=small_preflight_limits(),
        )

    assert result.receipt.classification == classification
    assert result.receipt.health_status_class == "2xx"
    assert result.receipt.ingest_status_class == "4xx"
    persisted = result.model_dump_json()
    assert endpoint not in persisted
    assert _SENTINEL_PATH not in persisted
    assert _SENTINEL_CREDENTIAL not in persisted


def integration_catalog(tmp_path: Path) -> IntegrationCatalog:
    return IntegrationCatalog(tmp_path / ".shea" / "artifacts" / "halo" / "integration-catalog")


def test_catalog_and_dependency_ranking_are_data_driven(tmp_path: Path) -> None:
    package = tmp_path / "apps" / "agent" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(
        json.dumps(
            {
                "dependencies": {"eve": "^0.24.4"},
                "devDependencies": {"@openai/agents": "^0.4.0"},
            }
        ),
        encoding="utf-8",
    )

    dependencies = discover_dependencies(tmp_path)
    ranked = rank_guides(parse_trace_catalog(CATALOG), dependencies)

    assert dependencies["eve"] == "^0.24.4"
    by_title = {guide.title: guide for guide in ranked}
    assert by_title["Vercel Eve Traces"].evidence == ("eve ^0.24.4",)
    assert set(by_title) == {
        "Vercel Eve Traces",
        "OpenAI Agents Traces",
    }


def test_dependency_scan_skips_runtime_and_install_trees(tmp_path: Path) -> None:
    ignored = tmp_path / "node_modules" / "fake" / "package.json"
    ignored.parent.mkdir(parents=True)
    ignored.write_text('{"dependencies":{"langchain":"bad"}}', encoding="utf-8")
    tracked = tmp_path / "services" / "worker" / "pyproject.toml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text(
        '[project]\nname="worker"\nversion="0.1.0"\ndependencies=["pydantic-ai==1.2"]\n',
        encoding="utf-8",
    )

    dependencies = discover_dependencies(tmp_path)

    assert dependencies == {"pydantic-ai": "pydantic-ai==1.2"}


def test_dependency_scan_reads_go_single_and_block_requirements(tmp_path: Path) -> None:
    manifest = tmp_path / "go.mod"
    manifest.write_text(
        """\
module example.com/agent

go 1.24

require go.opentelemetry.io/otel v1.35.0
require (
    github.com/openai/openai-go/v2 v2.6.0
)
""",
        encoding="utf-8",
    )

    dependencies = discover_dependencies(tmp_path)

    assert dependencies == {
        "github.com/openai/openai-go/v2": "v2.6.0",
        "go.opentelemetry.io/otel": "v1.35.0",
    }


def test_catalog_only_accepts_official_trace_markdown() -> None:
    guides = parse_trace_catalog(CATALOG)

    assert len(guides) == 3
    assert all(guide.url.startswith("https://docs.inference.net/") for guide in guides)


def test_guide_cache_is_explicitly_stale_when_network_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    catalog = integration_catalog(tmp_path)
    guide_url = "https://docs.inference.net/integrations/traces/eve.md"

    def online(url: str) -> str:
        return CATALOG if url.endswith("llms.txt") else "# Current Eve guide"

    monkeypatch.setattr(catalog, "_fetch", online)
    ref = next(guide for guide in catalog.load() if guide.url == guide_url)
    current = catalog.fetch_guide(ref)

    def offline(_url: str) -> str:
        raise IntegrationError("offline")

    monkeypatch.setattr(catalog, "_fetch", offline)
    stale = catalog.fetch_guide(ref)

    assert current.stale is False
    assert stale.stale is True
    assert stale.ref.stale is True
    assert stale.sha256 == current.sha256


def test_catalog_uses_only_the_official_machine_readable_index(tmp_path: Path) -> None:
    catalog = integration_catalog(tmp_path)

    assert catalog.catalog_url == INFERENCE_TRACE_CATALOG_URL

    with pytest.raises(IntegrationError, match="cache directory"):
        IntegrationCatalog(Path("https://example.com/catalog.txt"))


def test_catalog_rejects_unbounded_official_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = integration_catalog(tmp_path)

    class OversizedResponse:
        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(limit: int) -> bytes:
            assert limit == MAX_DOCUMENT_BYTES + 1
            return b"x" * limit

    monkeypatch.setattr(
        "shea_halo.integrations.urllib.request.urlopen",
        lambda *_args, **_kwargs: OversizedResponse(),
    )

    with pytest.raises(IntegrationError, match="exceeded"):
        catalog.load()


def test_cached_catalog_marks_every_guide_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = integration_catalog(tmp_path)
    monkeypatch.setattr(catalog, "_fetch", lambda _url: CATALOG)
    assert all(not guide.stale for guide in catalog.load())

    def offline(_url: str) -> str:
        raise IntegrationError("offline")

    monkeypatch.setattr(catalog, "_fetch", offline)
    cached = catalog.load()

    assert cached
    assert all(guide.stale for guide in cached)
    assert all(guide.stale for guide in rank_guides(cached, {"eve": "^0.24.4"}))


def test_stale_catalog_propagates_when_guide_fetch_itself_is_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = integration_catalog(tmp_path)
    monkeypatch.setattr(catalog, "_fetch", lambda _url: CATALOG)
    ref = next(
        guide
        for guide in catalog.load()
        if guide.url == "https://docs.inference.net/integrations/traces/eve.md"
    )

    def stale_catalog_fresh_guide(url: str) -> str:
        if url == INFERENCE_TRACE_CATALOG_URL:
            raise IntegrationError("catalog unavailable")
        return "# Fresh guide body"

    monkeypatch.setattr(catalog, "_fetch", stale_catalog_fresh_guide)
    fetched = catalog.fetch_guide(ref)

    assert fetched.content == "# Fresh guide body"
    assert fetched.stale is True
    assert fetched.ref.stale is True


def test_dependency_scan_rejects_manifest_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside-package.json"
    outside.write_text('{"dependencies":{"evil":"1.0.0"}}', encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / "package.json").symlink_to(outside)

    with pytest.raises(IntegrationError, match="escapes the target repository"):
        discover_dependencies(target)


def test_catalog_cache_rejects_symlink_leaf_before_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = integration_catalog(tmp_path)
    catalog.cache_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("protected", encoding="utf-8")
    (catalog.cache_dir / "llms.txt").symlink_to(outside)
    monkeypatch.setattr(catalog, "_fetch", lambda _url: CATALOG)

    with pytest.raises(IntegrationError, match="cache file is unsafe"):
        catalog.load()

    assert outside.read_text(encoding="utf-8") == "protected"


def test_guide_cache_rejects_symlink_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = integration_catalog(tmp_path)
    monkeypatch.setattr(catalog, "_fetch", lambda _url: CATALOG)
    ref = next(
        guide
        for guide in catalog.load()
        if guide.url == "https://docs.inference.net/integrations/traces/eve.md"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (catalog.cache_dir / "guides").symlink_to(outside, target_is_directory=True)

    with pytest.raises(IntegrationError, match="directory is unsafe"):
        catalog.fetch_guide(ref)

    assert list(outside.iterdir()) == []
