from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

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


class IntegrationError(RuntimeError):
    pass


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
            for line in manifest.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("//", "module", "go", "require", ")")):
                    name, _, version = stripped.partition(" ")
                    dependencies[name.lower()] = version.strip()

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
                return response.read().decode("utf-8")
        except OSError as exc:
            raise IntegrationError(f"failed to fetch {url}: {exc}") from exc

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
