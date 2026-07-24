from __future__ import annotations

import json
from pathlib import Path

import pytest

from shea_halo.integrations import (
    INFERENCE_TRACE_CATALOG_URL,
    MAX_DOCUMENT_BYTES,
    IntegrationCatalog,
    IntegrationError,
    discover_dependencies,
    parse_trace_catalog,
    rank_guides,
)

CATALOG = """\
# Docs
- [Vercel Eve Traces](https://docs.inference.net/integrations/traces/eve.md): Eve
- [OpenAI Agents Traces](https://docs.inference.net/integrations/traces/openai-agents.md): Agents
- [Manual Spans](https://docs.inference.net/integrations/traces/manual-spans.md): Manual
- [Unrelated](https://example.com/integrations/traces/eve.md): no
"""


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
