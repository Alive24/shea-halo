from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shea_halo.config import HaloConfig
from shea_halo.models import (
    BranchSnapshot,
    PublicationIntent,
    SnapshotPath,
)
from shea_halo.runtime_fs import RuntimeFilesystem, RuntimeFilesystemError
from shea_halo.security import (
    contains_host_absolute_path,
    contains_sensitive_text,
    git_environment,
    is_sensitive_path,
    redact_text,
)

_SLUG_PART = re.compile(r"[a-z0-9]+")
_GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
MAX_SNAPSHOT_FILE_BYTES = 10 * 1024 * 1024
_HALO_GIT_NAME = "Shea Halo"
_HALO_GIT_EMAIL = "shea-halo@users.noreply.github.com"


class WorkspaceError(RuntimeError):
    pass


def semantic_slug(title: str, *, limit: int = 64) -> str:
    parts = _SLUG_PART.findall(title.lower())
    if not parts:
        raise WorkspaceError("issue title cannot produce a semantic branch slug")
    selected: list[str] = []
    for part in parts:
        candidate = "-".join([*selected, part])
        if len(candidate) > limit:
            break
        selected.append(part)
    return "-".join(selected) if selected else parts[0][:limit]


def branch_name(issue_number: int, title: str) -> str:
    return f"halo/issue-{issue_number}-{semantic_slug(title)}"


def _repository_from_remote(remote: str) -> str | None:
    value = remote.strip()
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/")
    elif value.startswith("ssh://git@github.com/"):
        value = value.removeprefix("ssh://git@github.com/")
    else:
        return None
    return value.removesuffix("/").removesuffix(".git")


@dataclass(frozen=True)
class HaloWorkspace:
    path: Path
    branch: str
    base_revision: str


class WorkspaceManager:
    def __init__(self, config: HaloConfig) -> None:
        self.config = config
        self.runtime = RuntimeFilesystem(config.root)

    @staticmethod
    def _run(
        cwd: Path,
        args: list[str],
        *,
        check: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            env=git_environment() if environment is None else environment,
            check=False,
        )
        if check and process.returncode:
            detail = process.stderr.strip() or process.stdout.strip()
            safe_detail = redact_text(detail)
            safe_command = redact_text(" ".join(args[:3]))
            raise WorkspaceError(f"{safe_command} failed: {safe_detail}")
        return process

    @staticmethod
    def _run_bytes(cwd: Path, args: list[str]) -> bytes:
        process = subprocess.run(
            args,
            cwd=cwd,
            text=False,
            capture_output=True,
            env=git_environment(),
            check=False,
        )
        if process.returncode:
            detail = process.stderr.decode("utf-8", errors="replace").strip()
            safe_detail = redact_text(detail)
            safe_command = redact_text(" ".join(args[:3]))
            raise WorkspaceError(f"{safe_command} failed: {safe_detail}")
        return process.stdout

    def _validated_origin_push_url(self, path: Path) -> str:
        fetch_urls = [
            url
            for url in self._run(
                path,
                ["git", "remote", "get-url", "--all", "origin"],
            ).stdout.splitlines()
            if url
        ]
        push_urls = [
            url
            for url in self._run(
                path,
                ["git", "remote", "get-url", "--push", "--all", "origin"],
            ).stdout.splitlines()
            if url
        ]
        if len(fetch_urls) != 1 or len(push_urls) != 1:
            raise WorkspaceError("origin must resolve to one fetch URL and one push URL")
        for url in (*fetch_urls, *push_urls):
            repository = _repository_from_remote(url)
            if repository is None or repository.casefold() != self.config.repository.casefold():
                raise WorkspaceError(
                    f"origin does not match configured repository {self.config.repository}"
                )
        remote_names = self._run(path, ["git", "remote"]).stdout.splitlines()
        if push_urls[0] in remote_names:
            raise WorkspaceError("origin push URL conflicts with a configured remote name")
        rewrites = self._run(
            path,
            [
                "git",
                "config",
                "-z",
                "--get-regexp",
                r"^url\..*\.(insteadof|pushinsteadof)$",
            ],
            check=False,
        )
        if rewrites.returncode not in {0, 1}:
            raise WorkspaceError("failed to inspect Git URL rewrite configuration")
        for record in rewrites.stdout.split("\0"):
            if not record:
                continue
            _, separator, prefix = record.partition("\n")
            if not separator:
                raise WorkspaceError("Git returned malformed URL rewrite configuration")
            if push_urls[0].startswith(prefix):
                raise WorkspaceError("origin push URL is subject to another Git URL rewrite")
        return push_urls[0]

    def _validate_source(self) -> str:
        root = self.config.root
        top = self._run(root, ["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(top).resolve() != root:
            raise WorkspaceError("target root is not the canonical Git checkout root")
        return self._validated_origin_push_url(root)

    def current_base_revision(self) -> str:
        self._validate_source()
        self._run(
            self.config.root,
            ["git", "fetch", "--no-tags", "origin", self.config.base_branch],
        )
        revision = self._run(
            self.config.root,
            ["git", "rev-parse", f"origin/{self.config.base_branch}"],
        ).stdout.strip()
        if not _GIT_OBJECT_ID.fullmatch(revision):
            raise WorkspaceError("configured base branch resolved to an invalid revision")
        return revision

    def _worktree_path(self, issue_number: int, branch: str) -> Path:
        expected_prefix = f"halo/issue-{issue_number}-"
        if not branch.startswith(expected_prefix):
            raise WorkspaceError("persisted branch does not belong to this Halo issue")
        leaf = branch.removeprefix("halo/")
        leaf_path = Path(leaf)
        if leaf_path.name != leaf or ".." in leaf_path.parts:
            raise WorkspaceError("Halo worktree path escapes its managed root")
        try:
            root = self.runtime.ensure_directory(self.config.worktrees_dir)
            candidate = self.runtime.path(root / leaf)
        except RuntimeFilesystemError:
            raise WorkspaceError("managed Halo worktrees root is unsafe") from None
        if candidate.parent != root:
            raise WorkspaceError("Halo worktree path escapes its managed root")
        return candidate

    def _existing_worktree(self, path: Path) -> bool:
        try:
            self.runtime.validate_directory(path)
        except FileNotFoundError:
            try:
                self.runtime.require_absent(path)
            except RuntimeFilesystemError:
                raise WorkspaceError("managed Halo worktree path is unsafe") from None
            return False
        except RuntimeFilesystemError:
            raise WorkspaceError("managed Halo worktree path is unsafe") from None
        return True

    def _validate_created_worktree(self, path: Path) -> None:
        try:
            self.runtime.validate_directory(path)
        except (FileNotFoundError, RuntimeFilesystemError):
            raise WorkspaceError("Git did not create a safe managed Halo worktree") from None

    def _git_common_directory(self, path: Path) -> Path:
        raw = self._run(path, ["git", "rev-parse", "--git-common-dir"]).stdout.strip()
        common = Path(raw)
        if not common.is_absolute():
            common = path / common
        try:
            return common.resolve(strict=True)
        except OSError:
            raise WorkspaceError(
                "managed Halo worktree has an unavailable Git common dir"
            ) from None

    def _validate_attempt_recovery_state(
        self,
        workspace: HaloWorkspace,
        *,
        issue_number: int,
        persisted_base_revision: str,
        expected_snapshot: BranchSnapshot | None,
        require_clean: bool,
    ) -> tuple[Path, str]:
        self._validate_source()
        if not _GIT_OBJECT_ID.fullmatch(persisted_base_revision):
            raise WorkspaceError("persisted Halo base revision is invalid")
        if workspace.base_revision != persisted_base_revision:
            raise WorkspaceError("managed Halo worktree base differs from persisted state")

        expected_path = self._worktree_path(issue_number, workspace.branch)
        try:
            verified_path = self.runtime.validate_directory(expected_path)
            supplied_path = workspace.path.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeFilesystemError):
            raise WorkspaceError("managed Halo worktree path is unsafe") from None
        if supplied_path != verified_path:
            raise WorkspaceError("managed Halo worktree path does not match its Halo-owned path")

        top = self._run(verified_path, ["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(top).resolve() != verified_path:
            raise WorkspaceError("managed Halo worktree has unexpected Git identity")
        if self._git_common_directory(verified_path) != self._git_common_directory(
            self.config.root
        ):
            raise WorkspaceError("managed Halo worktree is not owned by the configured checkout")
        current_branch = self._run(
            verified_path,
            ["git", "branch", "--show-current"],
        ).stdout.strip()
        if current_branch != workspace.branch:
            raise WorkspaceError("managed Halo worktree has unexpected Git identity")

        base_check = self._run(
            verified_path,
            ["git", "cat-file", "-e", f"{persisted_base_revision}^{{commit}}"],
            check=False,
        )
        if base_check.returncode:
            raise WorkspaceError("persisted Halo base revision is unavailable")

        if expected_snapshot is None:
            expected_head = persisted_base_revision
            expected_remote_head = None
        else:
            if expected_snapshot.name != workspace.branch:
                raise WorkspaceError("workpad snapshot branch does not match the managed worktree")
            if not _GIT_OBJECT_ID.fullmatch(expected_snapshot.head_revision):
                raise WorkspaceError("workpad snapshot revision is invalid")
            expected_head = expected_snapshot.head_revision
            expected_remote_head = expected_snapshot.head_revision

        ancestor = self._run(
            verified_path,
            [
                "git",
                "merge-base",
                "--is-ancestor",
                persisted_base_revision,
                expected_head,
            ],
            check=False,
        )
        if ancestor.returncode != 0:
            raise WorkspaceError("persisted Halo base revision is not an ancestor of snapshot HEAD")
        head = self._run(verified_path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if head != expected_head:
            raise WorkspaceError("managed Halo worktree HEAD differs from persisted state")
        if self._remote_head(verified_path, workspace.branch) != expected_remote_head:
            raise WorkspaceError("remote Halo branch differs from persisted snapshot")
        if require_clean and (self._snapshot_paths(workspace) or self._ignored_paths(workspace)):
            raise WorkspaceError("managed Halo worktree remained dirty after attempt recovery")
        return verified_path, expected_head

    def _ignored_paths(self, workspace: HaloWorkspace) -> list[str]:
        ignored = self._run(
            workspace.path,
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
        )
        return sorted(path for path in ignored.stdout.split("\0") if path)

    def _clean_ignored_paths(self, workspace: HaloWorkspace) -> None:
        """Remove every ignored path from the experiment worktree."""

        self._run(
            workspace.path,
            [
                "git",
                "clean",
                "--force",
                "--force",
                "-d",
                "-X",
                "--",
            ],
        )
        remaining = self._ignored_paths(workspace)
        if remaining:
            raise WorkspaceError(
                f"ignored state remained in the validation worktree: {redact_text(remaining[0])}"
            )

    def discard_unpublished_attempt(
        self,
        workspace: HaloWorkspace,
        *,
        issue_number: int,
        persisted_base_revision: str,
        expected_snapshot: BranchSnapshot | None,
    ) -> HaloWorkspace:
        """Discard one failed attempt without changing durable Git state.

        This primitive applies only to the exact Halo-owned managed worktree
        represented by persisted state. It resets tracked state to the
        persisted base or snapshot HEAD, removes non-ignored untracked paths,
        and removes all ignored state in that worktree. Target-owned durable
        Halo artifacts live in the observed checkout; worktree dependencies
        and observations are rebuilt by setup and experiments.
        """

        path, expected_head = self._validate_attempt_recovery_state(
            workspace,
            issue_number=issue_number,
            persisted_base_revision=persisted_base_revision,
            expected_snapshot=expected_snapshot,
            require_clean=False,
        )
        self._run(
            path,
            [
                "git",
                "restore",
                f"--source={expected_head}",
                "--staged",
                "--",
                ".",
            ],
        )
        self._run(
            path,
            [
                "git",
                "restore",
                f"--source={expected_head}",
                "--worktree",
                "--",
                ".",
            ],
        )
        self._run(path, ["git", "clean", "--force", "-d", "--"])
        self._clean_ignored_paths(workspace)
        recovered_path, _ = self._validate_attempt_recovery_state(
            workspace,
            issue_number=issue_number,
            persisted_base_revision=persisted_base_revision,
            expected_snapshot=expected_snapshot,
            require_clean=True,
        )
        return HaloWorkspace(
            path=recovered_path,
            branch=workspace.branch,
            base_revision=persisted_base_revision,
        )

    def recover_abandoned_attempt(
        self,
        *,
        issue_number: int,
        persisted_branch: str,
        persisted_base_revision: str,
        expected_snapshot: BranchSnapshot | None,
    ) -> HaloWorkspace:
        """Recover a kill-interrupted attempt that never reached publication intent."""

        workspace = HaloWorkspace(
            path=self._worktree_path(issue_number, persisted_branch),
            branch=persisted_branch,
            base_revision=persisted_base_revision,
        )
        return self.discard_unpublished_attempt(
            workspace,
            issue_number=issue_number,
            persisted_base_revision=persisted_base_revision,
            expected_snapshot=expected_snapshot,
        )

    def prepare_validation_workspace(
        self,
        workspace: HaloWorkspace,
        *,
        issue_number: int,
        persisted_base_revision: str,
        expected_snapshot: BranchSnapshot | None,
    ) -> HaloWorkspace:
        """Make candidate validation depend only on the published Git snapshot.

        Tracked and non-ignored state must already be clean. Every ignored
        worktree path, including worktree-local `.shea`, is discarded before
        target-owned setup and experiments rebuild their exact inputs.
        """

        path, _ = self._validate_attempt_recovery_state(
            workspace,
            issue_number=issue_number,
            persisted_base_revision=persisted_base_revision,
            expected_snapshot=expected_snapshot,
            require_clean=False,
        )
        if self._snapshot_paths(workspace):
            raise WorkspaceError("candidate validation cannot start with unrecorded source changes")
        self._clean_ignored_paths(workspace)
        verified_path, _ = self._validate_attempt_recovery_state(
            workspace,
            issue_number=issue_number,
            persisted_base_revision=persisted_base_revision,
            expected_snapshot=expected_snapshot,
            require_clean=True,
        )
        return HaloWorkspace(
            path=verified_path,
            branch=workspace.branch,
            base_revision=persisted_base_revision,
        )

    def prepare(
        self,
        *,
        issue_number: int,
        title: str,
        persisted_branch: str | None = None,
        persisted_base_revision: str | None = None,
        expected_snapshot: BranchSnapshot | None = None,
        publication_intent: PublicationIntent | None = None,
        allow_existing: bool = False,
    ) -> HaloWorkspace:
        self._validate_source()
        branch = persisted_branch or branch_name(issue_number, title)
        check_ref = self._run(
            self.config.root,
            ["git", "check-ref-format", "--branch", branch],
            check=False,
        )
        if check_ref.returncode:
            raise WorkspaceError("generated branch name is not a valid Git branch")
        path = self._worktree_path(issue_number, branch)
        path_exists = self._existing_worktree(path)

        base_revision = self.current_base_revision()

        if path_exists:
            if not allow_existing:
                raise WorkspaceError("managed Halo worktree already exists without workpad state")
            top = self._run(path, ["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            current = self._run(path, ["git", "branch", "--show-current"]).stdout.strip()
            if Path(top).resolve() != path or current != branch:
                raise WorkspaceError("existing managed worktree has unexpected Git identity")
            restored_base = persisted_base_revision or self._merge_base(path, base_revision)
            self._validate_restored_state(
                path,
                branch=branch,
                base_revision=restored_base,
                expected_snapshot=expected_snapshot,
                publication_intent=publication_intent,
            )
            return HaloWorkspace(path=path, branch=branch, base_revision=restored_base)

        local_branch = (
            self._run(
                self.config.root,
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        )
        remote_branch = (
            self._run(
                self.config.root,
                ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
                check=False,
            ).returncode
            == 0
        )

        if (local_branch or remote_branch) and not allow_existing:
            raise WorkspaceError("Halo branch already exists without a trusted workpad history")

        if local_branch:
            self._run(self.config.root, ["git", "worktree", "add", str(path), branch])
        elif remote_branch:
            self._run(
                self.config.root,
                ["git", "fetch", "--no-tags", "origin", branch],
            )
            self._run(
                self.config.root,
                [
                    "git",
                    "worktree",
                    "add",
                    "--track",
                    "-b",
                    branch,
                    str(path),
                    f"origin/{branch}",
                ],
            )
        else:
            self._run(
                self.config.root,
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    f"origin/{self.config.base_branch}",
                ],
            )
        self._validate_created_worktree(path)
        restored_base = persisted_base_revision or (
            self._merge_base(path, base_revision)
            if local_branch or remote_branch
            else base_revision
        )
        if allow_existing:
            self._validate_restored_state(
                path,
                branch=branch,
                base_revision=restored_base,
                expected_snapshot=expected_snapshot,
                publication_intent=publication_intent,
            )
        return HaloWorkspace(path=path, branch=branch, base_revision=restored_base)

    def _merge_base(self, path: Path, current_base_revision: str) -> str:
        merge_base = self._run(
            path,
            ["git", "merge-base", "HEAD", current_base_revision],
        ).stdout.strip()
        if not merge_base:
            raise WorkspaceError("managed Halo branch has no base with the target branch")
        return merge_base

    def _validate_restored_state(
        self,
        path: Path,
        *,
        branch: str,
        base_revision: str,
        expected_snapshot: BranchSnapshot | None,
        publication_intent: PublicationIntent | None,
    ) -> None:
        base_check = self._run(
            path,
            ["git", "cat-file", "-e", f"{base_revision}^{{commit}}"],
            check=False,
        )
        if base_check.returncode:
            raise WorkspaceError("persisted Halo base revision is unavailable")
        ancestor = self._run(
            path,
            ["git", "merge-base", "--is-ancestor", base_revision, "HEAD"],
            check=False,
        )
        if ancestor.returncode != 0:
            raise WorkspaceError("persisted Halo base revision is not an ancestor of HEAD")
        head = self._run(path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if publication_intent is not None:
            self._validate_publication_state(
                HaloWorkspace(path=path, branch=branch, base_revision=base_revision),
                publication_intent,
                expected_snapshot=expected_snapshot,
            )
            return
        if expected_snapshot is None:
            if head != base_revision:
                raise WorkspaceError(
                    "unpublished Halo worktree HEAD differs from its persisted base"
                )
            if self._snapshot_paths(
                HaloWorkspace(path=path, branch=branch, base_revision=base_revision)
            ):
                raise WorkspaceError(
                    "unpublished Halo worktree contains changes without a publication intent"
                )
            if self._remote_head(path, branch) is not None:
                raise WorkspaceError(
                    "unpublished Halo branch exists remotely without a publication intent"
                )
            return
        if expected_snapshot.name != branch:
            raise WorkspaceError("workpad snapshot branch does not match the managed worktree")
        if head != expected_snapshot.head_revision:
            raise WorkspaceError("managed Halo worktree HEAD differs from its workpad snapshot")
        if self._snapshot_paths(
            HaloWorkspace(path=path, branch=branch, base_revision=base_revision)
        ):
            raise WorkspaceError("managed Halo worktree contains unrecorded changes")
        if self._remote_head(path, branch) != expected_snapshot.head_revision:
            raise WorkspaceError("remote Halo branch differs from its workpad snapshot")

    def _remote_head(
        self,
        path: Path,
        branch: str,
        remote: str = "origin",
    ) -> str | None:
        process = self._run(
            path,
            ["git", "ls-remote", "--heads", remote, branch],
            check=False,
        )
        if process.returncode not in {0, 2}:
            raise WorkspaceError("failed to inspect the remote Halo branch")
        output = process.stdout.split()
        if not output:
            return None
        if len(output) < 2:
            raise WorkspaceError("remote Halo branch response was malformed")
        return output[0]

    def _snapshot_paths(self, workspace: HaloWorkspace) -> list[str]:
        tracked = self._run(
            workspace.path,
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--no-textconv",
                "--name-only",
                "-z",
                "HEAD",
            ],
        )
        untracked = self._run(
            workspace.path,
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        )
        return sorted({path for path in (tracked.stdout + untracked.stdout).split("\0") if path})

    def _regular_file_exists_at_head(
        self,
        workspace: HaloWorkspace,
        relative: str,
    ) -> bool:
        rendered = self._run(
            workspace.path,
            [
                "git",
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                "--full-tree",
                "HEAD",
                "--",
                relative,
            ],
        ).stdout
        for entry in rendered.split("\0"):
            if "\t" not in entry:
                continue
            metadata, returned_path = entry.split("\t", 1)
            fields = metadata.split()
            if (
                returned_path == relative
                and len(fields) == 3
                and fields[0] in {"100644", "100755"}
                and fields[1] == "blob"
                and _GIT_OBJECT_ID.fullmatch(fields[2])
            ):
                return True
        return False

    def _added_snapshot_text(self, workspace: HaloWorkspace, relative: str) -> str:
        diff = self._run(
            workspace.path,
            [
                "git",
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--no-textconv",
                "--no-color",
                "--unified=0",
                "HEAD",
                "--",
                relative,
            ],
        ).stdout
        in_hunk = False
        added: list[str] = []
        for line in diff.splitlines():
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith("diff --git "):
                in_hunk = False
            elif in_hunk and line.startswith("+"):
                added.append(line[1:])
        return "\n".join(added)

    def _path_has_binary_diff(self, workspace: HaloWorkspace, relative: str) -> bool:
        rendered = self._run(
            workspace.path,
            [
                "git",
                "--literal-pathspecs",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--no-textconv",
                "--numstat",
                "-z",
                "HEAD",
                "--",
                relative,
            ],
        ).stdout
        return any(
            len(fields := record.split("\t", 2)) >= 2 and (fields[0] == "-" or fields[1] == "-")
            for record in rendered.split("\0")
            if record
        )

    def _canonical_transform_attributes(
        self,
        workspace: HaloWorkspace,
        relative: str,
        *,
        source: str | None = None,
    ) -> tuple[bool, bool]:
        args = [
            "git",
            "--literal-pathspecs",
            "check-attr",
            "-z",
            "--all",
        ]
        if source is not None:
            args.extend(["--source", source])
        args.extend(["--", relative])
        rendered = self._run(
            workspace.path,
            args,
        ).stdout
        fields = rendered.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) % 3:
            raise WorkspaceError("Git returned malformed canonical transform attributes")
        attributes: dict[str, str] = {}
        for index in range(0, len(fields), 3):
            returned_path, attribute, value = fields[index : index + 3]
            if returned_path != relative or attribute in attributes:
                raise WorkspaceError("Git returned unexpected canonical transform attributes")
            attributes[attribute] = value
        return (
            "filter" in attributes,
            "working-tree-encoding" in attributes,
        )

    def _validate_snapshot(self, workspace: HaloWorkspace, paths: list[str]) -> None:
        root = workspace.path.resolve()
        for relative in paths:
            relative_path = Path(relative)
            if ".shea" in relative_path.parts:
                raise WorkspaceError("refusing to snapshot Shea runtime or observation artifacts")
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise WorkspaceError("snapshot path escapes the managed Halo worktree")
            if not path.is_file():
                continue
            if is_sensitive_path(Path(relative)):
                raise WorkspaceError(
                    f"refusing to snapshot sensitive file: {redact_text(relative)}"
                )
            size = path.stat().st_size
            if size > MAX_SNAPSHOT_FILE_BYTES:
                raise WorkspaceError(
                    f"refusing to snapshot file larger than {MAX_SNAPSHOT_FILE_BYTES} bytes: "
                    f"{redact_text(relative)}"
                )
            current_filter, current_working_tree_encoding = self._canonical_transform_attributes(
                workspace,
                relative,
            )
            head_filter, head_working_tree_encoding = self._canonical_transform_attributes(
                workspace,
                relative,
                source="HEAD",
            )
            if current_filter or head_filter:
                raise WorkspaceError(
                    f"refusing to snapshot a path with a Git clean filter: {redact_text(relative)}"
                )
            if current_working_tree_encoding or head_working_tree_encoding:
                raise WorkspaceError(
                    f"refusing to snapshot a path with an opaque working-tree encoding: "
                    f"{redact_text(relative)}"
                )
            raw_content = path.read_bytes()
            if len(raw_content) > MAX_SNAPSHOT_FILE_BYTES:
                raise WorkspaceError(
                    f"refusing to snapshot file larger than {MAX_SNAPSHOT_FILE_BYTES} bytes: "
                    f"{redact_text(relative)}"
                )
            try:
                content = raw_content.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError(
                    f"refusing to snapshot opaque or binary content: {redact_text(relative)}"
                ) from None
            if "\0" in content:
                raise WorkspaceError(
                    f"refusing to snapshot opaque or binary content: {redact_text(relative)}"
                )
            if contains_sensitive_text(content):
                raise WorkspaceError(
                    f"refusing to snapshot file containing credential material: "
                    f"{redact_text(relative)}"
                )
            regular_at_head = self._regular_file_exists_at_head(workspace, relative)
            binary_diff = regular_at_head and self._path_has_binary_diff(workspace, relative)
            if binary_diff:
                raise WorkspaceError(
                    f"refusing to snapshot opaque or binary content: {redact_text(relative)}"
                )
            if regular_at_head and not binary_diff:
                canonical_added_text = self._added_snapshot_text(workspace, relative)
                if contains_sensitive_text(canonical_added_text):
                    raise WorkspaceError(
                        f"refusing to snapshot canonical content containing credential material: "
                        f"{redact_text(relative)}"
                    )
                if contains_host_absolute_path(canonical_added_text):
                    raise WorkspaceError(
                        f"refusing to snapshot a canonical host absolute path: "
                        f"{redact_text(relative)}"
                    )
            elif contains_host_absolute_path(content):
                raise WorkspaceError(
                    f"refusing to snapshot a file containing a host absolute path: "
                    f"{redact_text(relative)}"
                )

    @staticmethod
    def _manifest_digest(paths: list[SnapshotPath]) -> str:
        payload = json.dumps(
            [path.model_dump(mode="json") for path in paths],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _snapshot_manifest(
        self,
        workspace: HaloWorkspace,
        paths: list[str],
    ) -> list[SnapshotPath]:
        root = workspace.path.resolve()
        manifest: list[SnapshotPath] = []
        for relative in sorted(paths):
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise WorkspaceError("snapshot path escapes the managed Halo worktree")
            lexical_path = root / candidate
            if lexical_path.is_symlink():
                raise WorkspaceError(
                    f"refusing to snapshot a symbolic link: {redact_text(relative)}"
                )
            if not lexical_path.exists():
                manifest.append(SnapshotPath(path=relative, state="deleted"))
                continue
            resolved = lexical_path.resolve()
            if not resolved.is_relative_to(root):
                raise WorkspaceError("snapshot path escapes the managed Halo worktree")
            if not resolved.is_file():
                raise WorkspaceError(
                    f"refusing to snapshot a non-file path: {redact_text(relative)}"
                )
            digest = hashlib.sha256()
            size = 0
            with resolved.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            git_blob_oid = self._run(
                workspace.path,
                [
                    "git",
                    "hash-object",
                    f"--path={relative}",
                    "--",
                    relative,
                ],
            ).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git_blob_oid):
                raise WorkspaceError("Git returned an invalid canonical blob identity")
            manifest.append(
                SnapshotPath(
                    path=relative,
                    state="file",
                    sha256=digest.hexdigest(),
                    git_blob_oid=git_blob_oid,
                    size=size,
                    executable=bool(resolved.stat().st_mode & 0o111),
                )
            )
        return manifest

    def _validate_committed_manifest(
        self,
        workspace: HaloWorkspace,
        intent: PublicationIntent,
        *,
        head: str,
    ) -> None:
        for expected in intent.paths:
            rendered = self._run(
                workspace.path,
                [
                    "git",
                    "--literal-pathspecs",
                    "ls-tree",
                    "-z",
                    head,
                    "--",
                    expected.path,
                ],
            ).stdout
            entries = [entry for entry in rendered.split("\0") if entry]
            if expected.state == "deleted":
                if entries:
                    raise WorkspaceError(
                        "committed publication contains a path intended for deletion"
                    )
                continue
            if expected.git_blob_oid is None:
                raise WorkspaceError("publication intent omitted a canonical Git blob identity")
            if len(entries) != 1 or "\t" not in entries[0]:
                raise WorkspaceError("committed publication omitted an intended file")
            metadata, returned_path = entries[0].split("\t", 1)
            fields = metadata.split()
            expected_mode = "100755" if expected.executable else "100644"
            if (
                returned_path != expected.path
                or len(fields) != 3
                or fields[0] != expected_mode
                or fields[1] != "blob"
                or fields[2] != expected.git_blob_oid
            ):
                raise WorkspaceError(
                    "committed publication tree differs from the publication intent"
                )

    def _validate_commit_metadata(
        self,
        workspace: HaloWorkspace,
        head: str,
        *,
        expected_message: str,
        expected_parent: str,
    ) -> None:
        rendered = self._run_bytes(
            workspace.path,
            ["git", "cat-file", "commit", head],
        )
        try:
            commit = rendered.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("refusing to publish non-UTF-8 Git commit metadata") from None
        if contains_sensitive_text(commit) or contains_host_absolute_path(commit):
            raise WorkspaceError("refusing to publish unsafe Git commit metadata")
        headers, separator, message = commit.partition("\n\n")
        if not separator or message != f"{expected_message}\n":
            raise WorkspaceError("Git commit message differs from the publication intent")

        parsed_headers: list[tuple[str, str]] = []
        for line in headers.splitlines():
            if line.startswith(" "):
                if not parsed_headers:
                    raise WorkspaceError("Git commit contains malformed metadata headers")
                name, value = parsed_headers[-1]
                parsed_headers[-1] = (name, f"{value}\n{line}")
                continue
            name, separator, value = line.partition(" ")
            if not separator or not name or not value:
                raise WorkspaceError("Git commit contains malformed metadata headers")
            parsed_headers.append((name, value))

        def values(name: str) -> list[str]:
            return [value for header, value in parsed_headers if header == name]

        trees = values("tree")
        parents = values("parent")
        authors = values("author")
        committers = values("committer")
        if len(trees) != 1 or _GIT_OBJECT_ID.fullmatch(trees[0]) is None:
            raise WorkspaceError("Git commit contains ambiguous tree metadata")
        if parents != [expected_parent]:
            raise WorkspaceError("Git commit contains ambiguous parent metadata")
        identity = re.compile(
            rf"{re.escape(_HALO_GIT_NAME)} <{re.escape(_HALO_GIT_EMAIL)}> "
            r"[0-9]+ [+-][0-9]{4}"
        )
        if (
            len(authors) != 1
            or identity.fullmatch(authors[0]) is None
            or len(committers) != 1
            or identity.fullmatch(committers[0]) is None
        ):
            raise WorkspaceError("Git commit identity differs from the publication identity")

    def create_publication_intent(
        self,
        workspace: HaloWorkspace,
        *,
        expected_snapshot: BranchSnapshot | None,
    ) -> PublicationIntent | None:
        parent = self._run(workspace.path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        expected_parent = (
            expected_snapshot.head_revision
            if expected_snapshot is not None
            else workspace.base_revision
        )
        if parent != expected_parent:
            raise WorkspaceError("managed Halo worktree gained an unrecorded commit")
        paths = self._snapshot_paths(workspace)
        if not paths:
            return None
        self._validate_snapshot(workspace, paths)
        manifest = self._snapshot_manifest(workspace, paths)
        return PublicationIntent(
            branch=workspace.branch,
            parent_revision=parent,
            manifest_sha256=self._manifest_digest(manifest),
            paths=manifest,
        )

    def _validate_intent_manifest(
        self,
        workspace: HaloWorkspace,
        intent: PublicationIntent,
    ) -> None:
        if intent.branch != workspace.branch:
            raise WorkspaceError("publication intent targets another branch")
        if self._manifest_digest(intent.paths) != intent.manifest_sha256:
            raise WorkspaceError("publication intent manifest digest is invalid")
        actual = self._snapshot_manifest(
            workspace,
            [item.path for item in intent.paths],
        )
        if actual != intent.paths:
            raise WorkspaceError("worktree contents differ from the publication intent")

    def _validate_publication_state(
        self,
        workspace: HaloWorkspace,
        intent: PublicationIntent,
        *,
        expected_snapshot: BranchSnapshot | None,
        remote: str = "origin",
    ) -> None:
        self._validate_intent_manifest(workspace, intent)
        expected_parent = (
            expected_snapshot.head_revision
            if expected_snapshot is not None
            else workspace.base_revision
        )
        if intent.parent_revision != expected_parent:
            raise WorkspaceError("publication intent does not extend the recorded Halo state")
        head = self._run(workspace.path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        dirty_paths = self._snapshot_paths(workspace)
        intended_paths = sorted(item.path for item in intent.paths)
        if head == intent.parent_revision:
            if dirty_paths != intended_paths:
                raise WorkspaceError("pending publication has unexpected worktree changes")
        else:
            if dirty_paths:
                raise WorkspaceError("committed publication worktree is not clean")
            parent = self._run(workspace.path, ["git", "rev-parse", "HEAD^"]).stdout.strip()
            if parent != intent.parent_revision:
                raise WorkspaceError("pending publication commit has an unexpected parent")
            committed_paths = self._run(
                workspace.path,
                [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--no-renames",
                    "--no-textconv",
                    "--name-only",
                    "-z",
                    intent.parent_revision,
                    "HEAD",
                ],
            ).stdout.split("\0")
            if sorted(path for path in committed_paths if path) != intended_paths:
                raise WorkspaceError("pending publication commit differs from its intent")
            self._validate_committed_manifest(workspace, intent, head=head)
        remote_head = self._remote_head(workspace.path, workspace.branch, remote)
        allowed_remote_heads = {None, head}
        if expected_snapshot is not None:
            allowed_remote_heads.add(expected_snapshot.head_revision)
        if remote_head not in allowed_remote_heads:
            raise WorkspaceError("remote Halo branch differs from the publication intent")

    def publish_intent(
        self,
        workspace: HaloWorkspace,
        intent: PublicationIntent,
        *,
        issue_number: int,
        title: str,
        expected_snapshot: BranchSnapshot | None,
    ) -> BranchSnapshot:
        metadata = f"{workspace.branch}\n{title}"
        if contains_sensitive_text(metadata) or contains_host_absolute_path(metadata):
            raise WorkspaceError("refusing to snapshot unsafe Git metadata")
        commit_message = f"halo: investigate issue #{issue_number} — {title}"
        push_url = self._validate_source()
        if self._validated_origin_push_url(workspace.path) != push_url:
            raise WorkspaceError("managed Halo worktree resolves a different origin push URL")
        self._validate_publication_state(
            workspace,
            intent,
            expected_snapshot=expected_snapshot,
            remote=push_url,
        )
        head = self._run(workspace.path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        if head == intent.parent_revision:
            self._run(workspace.path, ["git", "add", "--all"])
            staged = self._run(
                workspace.path,
                ["git", "diff", "--cached", "--quiet"],
                check=False,
            )
            if staged.returncode != 1:
                raise WorkspaceError("publication intent did not produce a staged commit")
            commit_environment = git_environment()
            commit_environment.update(
                {
                    "GIT_AUTHOR_NAME": _HALO_GIT_NAME,
                    "GIT_AUTHOR_EMAIL": _HALO_GIT_EMAIL,
                    "GIT_COMMITTER_NAME": _HALO_GIT_NAME,
                    "GIT_COMMITTER_EMAIL": _HALO_GIT_EMAIL,
                }
            )
            self._run(
                workspace.path,
                [
                    "git",
                    "-c",
                    "i18n.commitEncoding=UTF-8",
                    "commit",
                    "--cleanup=verbatim",
                    "-m",
                    commit_message,
                ],
                environment=commit_environment,
            )

        if (
            self._validate_source() != push_url
            or self._validated_origin_push_url(workspace.path) != push_url
        ):
            raise WorkspaceError("origin changed while preparing the Halo publication")
        self._validate_publication_state(
            workspace,
            intent,
            expected_snapshot=expected_snapshot,
            remote=push_url,
        )
        head = self._run(workspace.path, ["git", "rev-parse", "HEAD"]).stdout.strip()
        self._validate_commit_metadata(
            workspace,
            head,
            expected_message=commit_message,
            expected_parent=intent.parent_revision,
        )
        publication_ref = f"refs/shea-halo/publications/{head}"
        self._run(
            workspace.path,
            ["git", "update-ref", publication_ref, head],
        )
        if (
            self._run(
                workspace.path,
                ["git", "rev-parse", "--verify", publication_ref],
            ).stdout.strip()
            != head
        ):
            raise WorkspaceError("Halo publication ref differs from the validated commit")
        try:
            self._run(
                workspace.path,
                [
                    "git",
                    "push",
                    push_url,
                    f"{publication_ref}:refs/heads/{workspace.branch}",
                ],
            )
        except WorkspaceError:
            self._run(
                workspace.path,
                ["git", "update-ref", "-d", publication_ref, head],
                check=False,
            )
            raise
        self._run(
            workspace.path,
            ["git", "update-ref", "-d", publication_ref, head],
        )
        if self._remote_head(workspace.path, workspace.branch, push_url) != head:
            raise WorkspaceError("remote Halo branch did not reach the intended snapshot")
        if self._run(workspace.path, ["git", "rev-parse", "HEAD"]).stdout.strip() != head:
            raise WorkspaceError("managed Halo worktree HEAD changed during publication")
        return BranchSnapshot(name=workspace.branch, head_revision=head)
