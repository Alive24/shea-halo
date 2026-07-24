from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import run, write_target_config

from shea_halo.config import HaloConfig
from shea_halo.workspace import (
    HaloWorkspace,
    WorkspaceError,
    WorkspaceManager,
    _repository_from_remote,
    branch_name,
    semantic_slug,
)


def test_semantic_branch_name_is_stable_and_readable() -> None:
    assert semantic_slug("Improve Eve tracing coverage!") == "improve-eve-tracing-coverage"
    assert branch_name(42, "Improve Eve tracing coverage!") == (
        "halo/issue-42-improve-eve-tracing-coverage"
    )
    assert (
        _repository_from_remote("git@github.com:Alive24/FailureReport.git")
        == "Alive24/FailureReport"
    )
    assert semantic_slug(
        "Improve FailureReport trace coverage with framework-native instrumentation"
    ).endswith("native")


def test_source_validation_rejects_a_separate_origin_push_url(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run(source, "git", "init")
    run(
        source,
        "git",
        "remote",
        "add",
        "origin",
        "https://github.com/Alive24/FailureReport.git",
    )
    write_target_config(source)
    manager = WorkspaceManager(HaloConfig.load(source))

    assert manager._validate_source() == "https://github.com/Alive24/FailureReport.git"

    run(
        source,
        "git",
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "https://github.com/Alive24/another-repository.git",
    )
    with pytest.raises(WorkspaceError, match="configured repository"):
        manager._validate_source()


def test_source_validation_rejects_a_remote_named_like_the_push_url(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run(source, "git", "init")
    canonical_url = "https://github.com/Alive24/FailureReport.git"
    run(source, "git", "remote", "add", "origin", canonical_url)
    write_target_config(source)
    manager = WorkspaceManager(HaloConfig.load(source))
    run(
        source,
        "git",
        "config",
        f"remote.{canonical_url}.url",
        "https://github.com/Alive24/another-repository.git",
    )

    with pytest.raises(WorkspaceError, match="conflicts with a configured remote name"):
        manager._validate_source()


def test_workspace_validation_rejects_a_worktree_specific_url_rewrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run(source, "git", "init")
    run(source, "git", "config", "user.name", "Halo Test")
    run(source, "git", "config", "user.email", "halo@example.test")
    (source / "README.md").write_text("# target\n", encoding="utf-8")
    run(source, "git", "add", "README.md")
    run(source, "git", "commit", "-m", "initial")
    canonical_url = "https://github.com/Alive24/FailureReport.git"
    rewritten_url = "https://github.com/Alive24/another-repository.git"
    run(source, "git", "remote", "add", "origin", canonical_url)
    worktree = tmp_path / "managed-worktree"
    run(source, "git", "worktree", "add", "-b", "halo-test", str(worktree))
    run(source, "git", "config", "extensions.worktreeConfig", "true")
    run(
        worktree,
        "git",
        "config",
        "--worktree",
        f"url.{rewritten_url}.insteadOf",
        canonical_url,
    )
    write_target_config(source)
    manager = WorkspaceManager(HaloConfig.load(source))

    assert manager._validate_source() == canonical_url
    with pytest.raises(WorkspaceError, match="configured repository"):
        manager._validated_origin_push_url(worktree)


def test_source_validation_rejects_a_second_url_rewrite_pass(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run(source, "git", "init")
    canonical_url = "https://github.com/Alive24/FailureReport.git"
    run(source, "git", "remote", "add", "origin", "halo-source")
    run(
        source,
        "git",
        "config",
        f"url.{canonical_url}.insteadOf",
        "halo-source",
    )
    run(
        source,
        "git",
        "config",
        "url.file:///private/tmp/attacker.git.insteadOf",
        canonical_url,
    )
    write_target_config(source)
    manager = WorkspaceManager(HaloConfig.load(source))

    with pytest.raises(WorkspaceError, match="subject to another Git URL rewrite"):
        manager._validate_source()


def test_managed_worktree_commit_and_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    run(tmp_path, "git", "init", "--bare", str(remote))
    run(tmp_path, "git", "clone", str(remote), str(source))
    run(source, "git", "config", "user.name", "Halo Test")
    run(source, "git", "config", "user.email", "halo@example.test")
    run(source, "git", "checkout", "-b", "main")
    (source / ".gitignore").write_text(".shea/worktrees/\n", encoding="utf-8")
    (source / "README.md").write_text("# target\n", encoding="utf-8")
    run(source, "git", "add", ".")
    run(source, "git", "commit", "-m", "initial")
    run(source, "git", "push", "-u", "origin", "main")
    write_target_config(source)
    config = HaloConfig.load(source)
    manager = WorkspaceManager(config)
    monkeypatch.setattr(manager, "_validate_source", lambda: str(remote))
    monkeypatch.setattr(
        manager,
        "_validated_origin_push_url",
        lambda _path: str(remote),
    )

    workspace = manager.prepare(
        issue_number=20,
        title="Improve Eve tracing",
    )
    run(workspace.path, "git", "config", "user.name", "Halo Test")
    run(workspace.path, "git", "config", "user.email", "halo@example.test")
    run(
        workspace.path,
        "git",
        "config",
        "user.name",
        "api_token=literal-production-secret",
    )
    (workspace.path / "instrumentation.ts").write_text("export {};\n", encoding="utf-8")

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )

    assert snapshot.name == "halo/issue-20-improve-eve-tracing"
    assert (
        run(remote, "git", "rev-parse", "refs/heads/halo/issue-20-improve-eve-tracing")
        == snapshot.head_revision
    )
    assert run(
        workspace.path,
        "git",
        "show",
        "-s",
        "--format=%an%n%ae%n%cn%n%ce",
        snapshot.head_revision,
    ).splitlines() == [
        "Shea Halo",
        "shea-halo@users.noreply.github.com",
        "Shea Halo",
        "shea-halo@users.noreply.github.com",
    ]

    restored = manager.prepare(
        issue_number=20,
        title="A renamed Issue does not change the branch",
        persisted_branch=workspace.branch,
        persisted_base_revision=workspace.base_revision,
        expected_snapshot=snapshot,
        allow_existing=True,
    )
    assert restored.base_revision == workspace.base_revision


def test_existing_workspace_requires_trusted_workpad_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)

    with pytest.raises(WorkspaceError, match="without workpad state"):
        manager.prepare(
            issue_number=20,
            title="Improve Eve tracing",
            persisted_branch=workspace.branch,
        )


def test_restored_workspace_rejects_head_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "instrumentation.ts").write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    (workspace.path / "tamper.txt").write_text("tampered\n", encoding="utf-8")
    run(workspace.path, "git", "add", "tamper.txt")
    run(workspace.path, "git", "commit", "-m", "external tamper")

    with pytest.raises(WorkspaceError, match="HEAD differs"):
        manager.prepare(
            issue_number=20,
            title="Improve Eve tracing",
            persisted_branch=workspace.branch,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=snapshot,
            allow_existing=True,
        )


def test_restored_workspace_rejects_remote_branch_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "instrumentation.ts").write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    run(
        remote,
        "git",
        "update-ref",
        f"refs/heads/{workspace.branch}",
        workspace.base_revision,
    )

    with pytest.raises(WorkspaceError, match="remote Halo branch differs"):
        manager.prepare(
            issue_number=20,
            title="Improve Eve tracing",
            persisted_branch=workspace.branch,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=snapshot,
            allow_existing=True,
        )


def test_restored_workspace_rejects_unrecorded_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "unrecorded.txt").write_text("not trusted\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="without a publication intent"):
        manager.prepare(
            issue_number=20,
            title="Improve Eve tracing",
            persisted_branch=workspace.branch,
            persisted_base_revision=workspace.base_revision,
            allow_existing=True,
        )


def test_discard_unpublished_attempt_restores_base_and_cleans_ignored_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    _ignore_attempt_artifacts(workspace)
    readme = workspace.path / "README.md"
    readme.write_text("attempted change\n", encoding="utf-8")
    untracked = workspace.path / "attempt/output.txt"
    untracked.parent.mkdir()
    untracked.write_text("discard me\n", encoding="utf-8")
    artifact = workspace.path / ".shea/artifacts/halo/traces/attempt.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    dependency = workspace.path / "node_modules/example/index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("export {};\n", encoding="utf-8")
    run(
        workspace.path,
        "git",
        "add",
        "--force",
        ".shea/artifacts/halo/traces/attempt.jsonl",
        "node_modules/example/index.js",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    (workspace.path / "attempt-link").symlink_to(outside, target_is_directory=True)
    branch_before = run(workspace.path, "git", "branch", "--show-current")
    remote_before = manager._remote_head(workspace.path, workspace.branch)

    recovered = manager.discard_unpublished_attempt(
        workspace,
        issue_number=20,
        persisted_base_revision=workspace.base_revision,
        expected_snapshot=None,
    )

    assert recovered == workspace
    assert readme.read_text(encoding="utf-8") == "# target\n"
    assert not untracked.exists()
    assert not (workspace.path / "attempt-link").exists()
    assert not artifact.exists()
    assert not dependency.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert run(workspace.path, "git", "branch", "--show-current") == branch_before
    assert manager._remote_head(workspace.path, workspace.branch) == remote_before
    assert manager._snapshot_paths(workspace) == []


def test_prepare_validation_workspace_removes_all_worktree_ignored_state_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    _ignore_attempt_artifacts(workspace)
    target_artifact = manager.config.root / ".shea/artifacts/halo/traces/durable-target-trace.jsonl"
    target_artifact.parent.mkdir(parents=True, exist_ok=True)
    target_artifact.write_text('{"target":"durable"}\n', encoding="utf-8")
    worktree_artifact = workspace.path / ".shea/artifacts/halo/traces/transient.jsonl"
    worktree_artifact.parent.mkdir(parents=True)
    worktree_artifact.write_text('{"worktree":"transient"}\n', encoding="utf-8")
    dependency = workspace.path / "node_modules/example/index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("export {};\n", encoding="utf-8")

    prepared = manager.prepare_validation_workspace(
        workspace,
        issue_number=20,
        persisted_base_revision=workspace.base_revision,
        expected_snapshot=None,
    )

    assert prepared == workspace
    assert not worktree_artifact.exists()
    assert not dependency.exists()
    assert target_artifact.read_text(encoding="utf-8") == '{"target":"durable"}\n'
    assert manager._ignored_paths(workspace) == []


def test_recover_abandoned_attempt_discards_unrecorded_attempt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    _ignore_attempt_artifacts(workspace)
    (workspace.path / "README.md").write_text("interrupted\n", encoding="utf-8")
    untracked = workspace.path / "attempt.txt"
    untracked.write_text("interrupted\n", encoding="utf-8")
    ignored = workspace.path / "node_modules/example/index.js"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("interrupted\n", encoding="utf-8")

    recovered = manager.recover_abandoned_attempt(
        issue_number=20,
        persisted_branch=workspace.branch,
        persisted_base_revision=workspace.base_revision,
        expected_snapshot=None,
    )

    assert recovered == workspace
    assert (workspace.path / "README.md").read_text(encoding="utf-8") == "# target\n"
    assert not untracked.exists()
    assert not ignored.exists()
    assert manager._snapshot_paths(workspace) == []
    assert manager._ignored_paths(workspace) == []


def test_discard_unpublished_attempt_restores_persisted_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    candidate.write_text("attempted replacement\n", encoding="utf-8")
    untracked = workspace.path / "attempt.txt"
    untracked.write_text("discard me\n", encoding="utf-8")

    recovered = manager.discard_unpublished_attempt(
        workspace,
        issue_number=20,
        persisted_base_revision=workspace.base_revision,
        expected_snapshot=snapshot,
    )

    assert recovered == workspace
    assert candidate.read_text(encoding="utf-8") == "export {};\n"
    assert not untracked.exists()
    assert run(workspace.path, "git", "rev-parse", "HEAD") == snapshot.head_revision
    assert manager._remote_head(workspace.path, workspace.branch) == snapshot.head_revision


def test_discard_unpublished_attempt_rejects_wrong_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    tamper = workspace.path / "tamper.txt"
    tamper.write_text("committed outside Halo\n", encoding="utf-8")
    run(workspace.path, "git", "add", "tamper.txt")
    run(workspace.path, "git", "commit", "-m", "external commit")
    tampered_head = run(workspace.path, "git", "rev-parse", "HEAD")

    with pytest.raises(WorkspaceError, match="HEAD differs from persisted state"):
        manager.discard_unpublished_attempt(
            workspace,
            issue_number=20,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=None,
        )

    assert run(workspace.path, "git", "rev-parse", "HEAD") == tampered_head
    assert tamper.read_text(encoding="utf-8") == "committed outside Halo\n"


def test_discard_unpublished_attempt_rejects_non_managed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    forged = HaloWorkspace(
        path=outside,
        branch=workspace.branch,
        base_revision=workspace.base_revision,
    )

    with pytest.raises(WorkspaceError, match="does not match its Halo-owned path"):
        manager.discard_unpublished_attempt(
            forged,
            issue_number=20,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=None,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_discard_unpublished_attempt_rejects_wrong_remote_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    candidate.write_text("unpublished attempt\n", encoding="utf-8")
    run(
        remote,
        "git",
        "update-ref",
        f"refs/heads/{workspace.branch}",
        workspace.base_revision,
    )

    with pytest.raises(WorkspaceError, match="remote Halo branch differs"):
        manager.discard_unpublished_attempt(
            workspace,
            issue_number=20,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=snapshot,
        )

    assert candidate.read_text(encoding="utf-8") == "unpublished attempt\n"
    assert run(workspace.path, "git", "rev-parse", "HEAD") == snapshot.head_revision


def test_publication_intent_recovers_after_push_before_workpad_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "instrumentation.ts").write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None

    first = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    restored = manager.prepare(
        issue_number=20,
        title="Improve Eve tracing",
        persisted_branch=workspace.branch,
        persisted_base_revision=workspace.base_revision,
        publication_intent=intent,
        allow_existing=True,
    )
    recovered = manager.publish_intent(
        restored,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )

    assert recovered == first


def test_publication_intent_rejects_changes_after_it_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    candidate.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="differ from the publication intent"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )


def test_publication_revalidates_commit_after_pre_commit_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    hook = Path(run(workspace.path, "git", "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = workspace.path / hook
    hook.write_text(
        "#!/bin/sh\nprintf 'tampered\\n' > instrumentation.ts\ngit add instrumentation.ts\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with pytest.raises(WorkspaceError, match="contents differ"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


def test_publication_rejects_commit_message_hook_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    hook = Path(run(workspace.path, "git", "rev-parse", "--git-path", "hooks/commit-msg"))
    if not hook.is_absolute():
        hook = workspace.path / hook
    hook.write_text(
        "#!/bin/sh\nprintf 'api_token=literal-production-secret\\n' > \"$1\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with pytest.raises(WorkspaceError, match="unsafe Git commit metadata"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


@pytest.mark.parametrize(
    "hook_command",
    [
        "git remote set-url origin",
        "git remote set-url --add --push origin",
    ],
)
def test_publication_rejects_origin_retargeting_from_a_commit_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_command: str,
) -> None:
    remote, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attacker = tmp_path / "attacker.git"
    run(tmp_path, "git", "init", "--bare", str(attacker))

    def validate_local_origin() -> str:
        expected = str(remote)
        fetch_urls = manager._run(
            manager.config.root,
            ["git", "remote", "get-url", "--all", "origin"],
        ).stdout.splitlines()
        push_urls = manager._run(
            manager.config.root,
            ["git", "remote", "get-url", "--push", "--all", "origin"],
        ).stdout.splitlines()
        if fetch_urls != [expected] or push_urls != [expected]:
            raise WorkspaceError("origin does not match the configured repository")
        return expected

    monkeypatch.setattr(manager, "_validate_source", validate_local_origin)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    hook = Path(run(workspace.path, "git", "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = workspace.path / hook
    hook.write_text(
        f"#!/bin/sh\n{hook_command} '{attacker}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with pytest.raises(WorkspaceError, match="configured repository"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", str(remote), workspace.branch],
        ).stdout
        == ""
    )
    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", str(attacker), workspace.branch],
        ).stdout
        == ""
    )


def test_publication_rejects_a_recovered_commit_with_unsafe_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    run(workspace.path, "git", "add", candidate.name)
    run(
        workspace.path,
        "git",
        "-c",
        "user.name=api_token=literal-production-secret",
        "-c",
        "user.email=halo@example.test",
        "commit",
        "-m",
        "halo: investigate issue #20 — Improve Eve tracing",
    )

    with pytest.raises(WorkspaceError, match="unsafe Git commit metadata"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


def test_publication_rejects_a_recovered_commit_with_duplicate_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    run(workspace.path, "git", "add", candidate.name)
    tree = run(workspace.path, "git", "write-tree")
    commit_message = "halo: investigate issue #20 — Improve Eve tracing"
    created = _store_raw_commit(
        workspace,
        [
            f"tree {tree}",
            f"parent {intent.parent_revision}",
            "author Other Author <other@example.test> 1700000000 +0000",
            ("author Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
            ("committer Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
        ],
        commit_message,
    )
    run(workspace.path, "git", "reset", "--hard", created)

    with pytest.raises(WorkspaceError, match="identity differs"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


def test_publication_rejects_a_recovered_commit_with_a_second_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    run(workspace.path, "git", "add", candidate.name)
    tree = run(workspace.path, "git", "write-tree")
    base_tree = run(workspace.path, "git", "rev-parse", f"{intent.parent_revision}^{{tree}}")
    unrelated_parent = _store_raw_commit(
        workspace,
        [
            f"tree {base_tree}",
            "author Other Author <other@example.test> 1699999999 +0000",
            "committer Other Author <other@example.test> 1699999999 +0000",
        ],
        "unrelated history",
    )
    created = _store_raw_commit(
        workspace,
        [
            f"tree {tree}",
            f"parent {intent.parent_revision}",
            f"parent {unrelated_parent}",
            ("author Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
            ("committer Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
        ],
        "halo: investigate issue #20 — Improve Eve tracing",
    )
    run(workspace.path, "git", "reset", "--hard", created)

    with pytest.raises(WorkspaceError, match="ambiguous parent"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


def test_publication_validates_the_raw_commit_instead_of_a_replace_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    run(workspace.path, "git", "add", candidate.name)
    tree = run(workspace.path, "git", "write-tree")
    message = "halo: investigate issue #20 — Improve Eve tracing"
    common = [f"tree {tree}", f"parent {intent.parent_revision}"]
    unsafe_commit = _store_raw_commit(
        workspace,
        [
            *common,
            "author Other Author <other@example.test> 1700000000 +0000",
            "committer Other Author <other@example.test> 1700000000 +0000",
        ],
        message,
    )
    safe_commit = _store_raw_commit(
        workspace,
        [
            *common,
            ("author Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
            ("committer Shea Halo <shea-halo@users.noreply.github.com> 1700000000 +0000"),
        ],
        message,
    )
    run(workspace.path, "git", "replace", unsafe_commit, safe_commit)
    run(workspace.path, "git", "reset", "--hard", unsafe_commit)

    with pytest.raises(WorkspaceError, match="identity differs"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert (
        manager._run(
            workspace.path,
            ["git", "ls-remote", "--heads", "origin", workspace.branch],
        ).stdout
        == ""
    )


def test_publication_pushes_the_validated_object_when_head_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    original_run = manager._run
    moved = False
    pushed_head: str | None = None

    def move_head_before_push(
        cwd: Path,
        args: list[str],
        *,
        check: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal moved, pushed_head
        if args[:2] == ["git", "push"] and not moved:
            moved = True
            source_ref = args[-1].partition(":")[0]
            pushed_head = run(workspace.path, "git", "rev-parse", "--verify", source_ref)
            run(workspace.path, "git", "reset", "--hard", workspace.base_revision)
        return original_run(cwd, args, check=check, environment=environment)

    monkeypatch.setattr(manager, "_run", move_head_before_push)

    with pytest.raises(WorkspaceError, match="HEAD changed during publication"):
        manager.publish_intent(
            workspace,
            intent,
            issue_number=20,
            title="Improve Eve tracing",
            expected_snapshot=None,
        )

    assert moved
    pushed = original_run(
        workspace.path,
        ["git", "ls-remote", "--heads", str(remote), workspace.branch],
    ).stdout.split()
    assert len(pushed) == 2
    assert pushed_head is not None
    assert pushed[0] == pushed_head


def test_publication_uses_an_unambiguous_ref_when_a_branch_is_named_like_the_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    candidate = workspace.path / "instrumentation.ts"
    candidate.write_text("export {};\n", encoding="utf-8")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    original_validate = manager._validate_commit_metadata

    def create_ambiguous_branch(
        validated_workspace: HaloWorkspace,
        head: str,
        *,
        expected_message: str,
        expected_parent: str,
    ) -> None:
        original_validate(
            validated_workspace,
            head,
            expected_message=expected_message,
            expected_parent=expected_parent,
        )
        run(
            workspace.path,
            "git",
            "branch",
            head,
            workspace.base_revision,
        )

    monkeypatch.setattr(manager, "_validate_commit_metadata", create_ambiguous_branch)
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )

    assert snapshot.head_revision != workspace.base_revision
    assert (
        run(remote, "git", "rev-parse", f"refs/heads/{workspace.branch}") == snapshot.head_revision
    )
    assert (
        manager._run(
            workspace.path,
            [
                "git",
                "rev-parse",
                "--verify",
                f"refs/shea-halo/publications/{snapshot.head_revision}",
            ],
            check=False,
        ).returncode
        != 0
    )


def _store_raw_commit(
    workspace: HaloWorkspace,
    headers: list[str],
    message: str,
) -> str:
    raw_commit = "\n".join([*headers, "", message, ""])
    return subprocess.run(
        ["git", "hash-object", "--literally", "-t", "commit", "-w", "--stdin"],
        cwd=workspace.path,
        input=raw_commit,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _prepared_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, WorkspaceManager, HaloWorkspace]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    run(tmp_path, "git", "init", "--bare", str(remote))
    run(tmp_path, "git", "clone", str(remote), str(source))
    run(source, "git", "config", "user.name", "Halo Test")
    run(source, "git", "config", "user.email", "halo@example.test")
    run(source, "git", "checkout", "-b", "main")
    (source / ".gitignore").write_text(".shea/worktrees/\n", encoding="utf-8")
    (source / "README.md").write_text("# target\n", encoding="utf-8")
    run(source, "git", "add", ".")
    run(source, "git", "commit", "-m", "initial")
    run(source, "git", "push", "-u", "origin", "main")
    write_target_config(source)
    manager = WorkspaceManager(HaloConfig.load(source))
    monkeypatch.setattr(manager, "_validate_source", lambda: str(remote))
    monkeypatch.setattr(
        manager,
        "_validated_origin_push_url",
        lambda _path: str(remote),
    )
    workspace = manager.prepare(issue_number=20, title="Improve Eve tracing")
    run(workspace.path, "git", "config", "user.name", "Halo Test")
    run(workspace.path, "git", "config", "user.email", "halo@example.test")
    return remote, manager, workspace


def _ignore_attempt_artifacts(workspace: HaloWorkspace) -> None:
    exclude = Path(run(workspace.path, "git", "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = workspace.path / exclude
    with exclude.open("a", encoding="utf-8") as output:
        output.write("\n.shea/artifacts/\nnode_modules/\n")
    assert run(
        workspace.path,
        "git",
        "check-ignore",
        ".shea/artifacts/halo/traces/attempt.jsonl",
    )
    assert run(workspace.path, "git", "check-ignore", "node_modules/example/index.js")


def _commit_snapshot_fixture(
    workspace: HaloWorkspace,
    *,
    content: str,
) -> tuple[HaloWorkspace, Path]:
    fixture = workspace.path / "existing-fixture.txt"
    fixture.write_text(content, encoding="utf-8")
    run(workspace.path, "git", "add", fixture.name)
    run(workspace.path, "git", "commit", "-m", "add existing fixture")
    head = run(workspace.path, "git", "rev-parse", "HEAD")
    return (
        HaloWorkspace(
            path=workspace.path,
            branch=workspace.branch,
            base_revision=head,
        ),
        fixture,
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.production",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "credentials.json",
        "identity.pem",
        "signing.key",
        "certificate.p12",
        "id_rsa",
    ],
)
def test_snapshot_rejects_sensitive_files_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / relative).write_text("sensitive\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="sensitive file"):
        manager.create_publication_intent(workspace, expected_snapshot=None)

    assert run(workspace.path, "git", "diff", "--cached", "--name-only") == ""


def test_snapshot_rejects_secret_content_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    secret = "sk-snapshot-environment-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    (workspace.path / "notes.txt").write_text(f"diagnostic={secret}\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="credential material") as error:
        manager.create_publication_intent(workspace, expected_snapshot=None)

    assert secret not in str(error.value)
    assert run(workspace.path, "git", "diff", "--cached", "--name-only") == ""


def test_snapshot_rejects_common_token_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "notes.txt").write_text(
        "\n".join(
            [
                "github_pat_abcdefghijklmnopqrstuvwxyz",
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
                'api_key = "literal-production-secret"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="credential material"):
        manager.create_publication_intent(workspace, expected_snapshot=None)

    assert run(workspace.path, "git", "diff", "--cached", "--name-only") == ""


def test_snapshot_accepts_dependency_names_containing_security_words(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "pnpm-lock.yaml").write_text(
        """\
packages:
  idb-keyval:
    optional: true
  token-types: 6.1.2
  path-key: 3.1.1
  cookie: 0.7.2
""",
        encoding="utf-8",
    )

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [path.path for path in intent.paths] == ["pnpm-lock.yaml"]


def test_snapshot_accepts_removing_a_preexisting_credential_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="raw_error_summary: token=ghp_not-a-real-token\n",
    )
    fixture.write_text("credential fixture removed\n", encoding="utf-8")

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [path.path for path in intent.paths] == [fixture.name]


def test_snapshot_accepts_deleting_a_preexisting_sensitive_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    fixture = workspace.path / ".env"
    fixture.write_text("API_KEY=synthetic-production-secret\n", encoding="utf-8")
    run(workspace.path, "git", "add", "--force", fixture.name)
    run(workspace.path, "git", "commit", "-m", "add sensitive fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    fixture.unlink()

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [(path.path, path.state) for path in intent.paths] == [(fixture.name, "deleted")]


def test_snapshot_rejects_safe_edits_while_a_preexisting_credential_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    credential_line = "raw_error_summary: token=ghp_not-a-real-token"
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content=f"{credential_line}\nvalue=before\n",
    )
    fixture.write_text(f"{credential_line}\nvalue=after\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="credential material"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_shea_runtime_artifacts_even_if_unignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    artifact = workspace.path / ".shea/artifacts/halo/traces/forged.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    (workspace.path / ".gitignore").write_text("", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Shea runtime"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_host_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "notes.md").write_text(
        "Debug output: /Users/alice/private/trace.jsonl\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="host absolute path"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_accepts_safe_edits_to_files_with_preexisting_unsafe_fixtures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    unsafe_line = "fixture: /Users/alice/private/trace.jsonl"
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content=f"{unsafe_line}\nvalue=before\n",
    )
    fixture.write_text(f"{unsafe_line}\nvalue=after\n", encoding="utf-8")

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [path.path for path in intent.paths] == [fixture.name]


@pytest.mark.parametrize(
    ("unsafe_line", "message"),
    [
        ("fixture: /Users/bob/private/trace.jsonl", "host absolute path"),
        ('api_key = "another-literal-production-secret"', "credential material"),
    ],
)
def test_snapshot_rejects_new_unsafe_values_in_existing_fixture_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_line: str,
    message: str,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="fixture: /Users/alice/private/trace.jsonl\n",
    )
    fixture.write_text(f"{unsafe_line}\n", encoding="utf-8")

    if message == "host absolute path":
        assert unsafe_line in manager._added_snapshot_text(workspace, fixture.name)
    with pytest.raises(WorkspaceError, match=message):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_accepts_removing_a_preexisting_unsafe_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="fixture: /Users/alice/private/trace.jsonl\n",
    )
    fixture.write_text("fixture removed\n", encoding="utf-8")

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [path.path for path in intent.paths] == [fixture.name]


def test_snapshot_rejects_renaming_a_preexisting_unsafe_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="fixture: /Users/alice/private/trace.jsonl\n",
    )
    fixture.rename(workspace.path / "renamed-fixture.txt")

    assert not manager._regular_file_exists_at_head(workspace, "renamed-fixture.txt")
    with pytest.raises(WorkspaceError, match="host absolute path"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_replacing_a_head_tree_with_an_unsafe_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    tree = workspace.path / "existing-fixture"
    nested = tree / "nested.txt"
    tree.mkdir()
    nested.write_text("safe fixture\n", encoding="utf-8")
    run(workspace.path, "git", "add", str(nested.relative_to(workspace.path)))
    run(workspace.path, "git", "commit", "-m", "add existing fixture tree")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    nested.unlink()
    tree.rmdir()
    tree.write_text(
        "fixture: /Users/alice/private/trace.jsonl\n",
        encoding="utf-8",
    )

    assert not manager._regular_file_exists_at_head(workspace, tree.name)
    with pytest.raises(WorkspaceError, match="host absolute path"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_accepts_renaming_a_safe_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="safe fixture\n",
    )
    renamed = workspace.path / "renamed-fixture.txt"
    fixture.rename(renamed)

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [(path.path, path.state) for path in intent.paths] == [
        (fixture.name, "deleted"),
        (renamed.name, "file"),
    ]
    snapshot = manager.publish_intent(
        workspace,
        intent,
        issue_number=20,
        title="Improve Eve tracing",
        expected_snapshot=None,
    )
    assert snapshot.head_revision == run(workspace.path, "git", "rev-parse", "HEAD")


def test_snapshot_rejects_a_private_key_completed_across_the_base_and_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    workspace, fixture = _commit_snapshot_fixture(
        workspace,
        content="-----BEGIN PRIVATE KEY-----\n",
    )
    fixture.write_text(
        "-----BEGIN PRIVATE KEY-----\nsynthetic-private-key-body\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="credential material"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_fails_closed_for_unsafe_content_in_a_binary_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "existing-fixture.txt"
    attributes.write_text(f"{fixture.name} -diff\n", encoding="utf-8")
    fixture.write_text(
        "fixture: /Users/alice/private/trace.jsonl\nvalue=before\n",
        encoding="utf-8",
    )
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add binary fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    fixture.write_text(
        "fixture: /Users/alice/private/trace.jsonl\nvalue=after\n",
        encoding="utf-8",
    )

    assert manager._path_has_binary_diff(workspace, fixture.name)
    with pytest.raises(WorkspaceError, match="opaque or binary"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_changed_paths_with_working_tree_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "encoded-fixture.txt"
    attributes.write_text(
        f"{fixture.name} working-tree-encoding=UTF-16\n",
        encoding="utf-8",
    )
    fixture.write_bytes("value: before\n".encode("utf-16"))
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add encoded fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    fixture.write_bytes(
        "value: after\nfixture: /Users/alice/private/trace.jsonl\n".encode("utf-16")
    )

    with pytest.raises(WorkspaceError, match="working-tree encoding"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_credential_changes_with_working_tree_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "encoded-fixture.txt"
    attributes.write_text(
        f"{fixture.name} working-tree-encoding=UTF-16\n",
        encoding="utf-8",
    )
    fixture.write_bytes("value: before\n".encode("utf-16"))
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add encoded fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    fixture.write_bytes('value: after\napi_key = "literal-production-secret"\n'.encode("utf-16"))

    with pytest.raises(WorkspaceError, match="working-tree encoding"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_removing_working_tree_encoding_while_changing_its_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "encoded-fixture.txt"
    attributes.write_text(
        f"{fixture.name} working-tree-encoding=UTF-16\n",
        encoding="utf-8",
    )
    fixture.write_bytes("value: before\n".encode("utf-16"))
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add encoded fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    attributes.write_text("", encoding="utf-8")
    fixture.write_bytes('api_key = "literal-production-secret"\n'.encode("utf-16"))

    with pytest.raises(WorkspaceError, match="working-tree encoding"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_unmarked_non_utf8_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "opaque.bin").write_bytes(
        'api_key = "literal-production-secret"\n'.encode("utf-16")
    )

    with pytest.raises(WorkspaceError, match="opaque or binary"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_a_new_file_with_opaque_working_tree_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    attributes.write_text(
        "encoded-fixture.txt working-tree-encoding=UTF-16\n",
        encoding="utf-8",
    )
    run(workspace.path, "git", "add", attributes.name)
    run(workspace.path, "git", "commit", "-m", "configure working tree encoding")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    (workspace.path / "encoded-fixture.txt").write_bytes(
        "fixture: /Users/alice/private/trace.jsonl\n".encode("utf-16")
    )

    with pytest.raises(WorkspaceError, match="opaque working-tree encoding"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_treats_a_sentinel_named_working_tree_encoding_as_opaque(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    attributes.write_text(
        "encoded-fixture.txt working-tree-encoding=unspecified\n",
        encoding="utf-8",
    )
    run(workspace.path, "git", "add", attributes.name)
    run(workspace.path, "git", "commit", "-m", "configure sentinel-named encoding")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    (workspace.path / "encoded-fixture.txt").write_text(
        "safe working-tree content\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="opaque working-tree encoding"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_changed_paths_with_clean_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "filtered-fixture.txt"
    attributes.write_text(f"{fixture.name} filter=unstable\n", encoding="utf-8")
    fixture.write_text("value: before\n", encoding="utf-8")
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add filtered fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    fixture.write_text("value: after\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Git clean filter"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_rejects_removing_a_clean_filter_while_changing_its_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    fixture = workspace.path / "filtered-fixture.txt"
    attributes.write_text(f"{fixture.name} filter=unstable\n", encoding="utf-8")
    fixture.write_text("value: before\n", encoding="utf-8")
    run(workspace.path, "git", "config", "filter.unstable.clean", "cat")
    run(workspace.path, "git", "config", "filter.unstable.smudge", "cat")
    run(workspace.path, "git", "add", attributes.name, fixture.name)
    run(workspace.path, "git", "commit", "-m", "add filtered fixture")
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    attributes.write_text("", encoding="utf-8")
    fixture.write_text("value: after\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Git clean filter"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


@pytest.mark.parametrize("driver", ["unspecified", "unset"])
def test_snapshot_rejects_clean_filter_names_that_match_git_attribute_sentinels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    attributes = workspace.path / ".gitattributes"
    attributes.write_text(f"*.txt filter={driver}\n", encoding="utf-8")
    run(workspace.path, "git", "add", attributes.name)
    run(workspace.path, "git", "commit", "-m", "configure sentinel-named filter")
    run(
        workspace.path,
        "git",
        "config",
        f"filter.{driver}.clean",
        "printf 'fixture: /Users/alice/private/trace.jsonl\\n'",
    )
    workspace = HaloWorkspace(
        path=workspace.path,
        branch=workspace.branch,
        base_revision=run(workspace.path, "git", "rev-parse", "HEAD"),
    )
    (workspace.path / "filtered-fixture.txt").write_text(
        "safe working-tree content\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="Git clean filter"):
        manager.create_publication_intent(workspace, expected_snapshot=None)


def test_snapshot_accepts_javascript_url_normalization_regex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / "tracing.mjs").write_text(
        "\n".join(
            [
                "#!/usr/bin/env node",
                'const endpoint = base.replace(/\\/$/, "") + "/v1/traces";',
                (
                    "const integrity = "
                    '"sha512-4+/OFSqOjoyULo7eN7EA97DE0Xydj/'
                    "PW5aIckxqQIoFjFwqXKuFCvXUJObyJfBF9Khu4RL/"
                    'jlDRI9FPaMGfPnw==";'
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )

    intent = manager.create_publication_intent(workspace, expected_snapshot=None)

    assert intent is not None
    assert [path.path for path in intent.paths] == ["tracing.mjs"]


def test_snapshot_rejects_large_files_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shea_halo.workspace as workspace_module

    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_module, "MAX_SNAPSHOT_FILE_BYTES", 16)
    (workspace.path / "large.bin").write_bytes(b"x" * 17)

    with pytest.raises(WorkspaceError, match="larger than 16 bytes"):
        manager.create_publication_intent(workspace, expected_snapshot=None)

    assert run(workspace.path, "git", "diff", "--cached", "--name-only") == ""


def test_worktrees_root_rejects_symlink_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    worktrees_parent = tmp_path / ".shea" / "worktrees"
    worktrees_parent.symlink_to(outside, target_is_directory=True)
    manager = WorkspaceManager(config)
    monkeypatch.setattr(manager, "_validate_source", lambda: None)

    with pytest.raises(WorkspaceError, match="worktrees root is unsafe"):
        manager.prepare(issue_number=20, title="Improve Eve tracing")

    assert list(outside.iterdir()) == []


def test_worktree_leaf_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    config.worktrees_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    leaf = config.worktrees_dir / "issue-20-improve-eve-tracing"
    leaf.symlink_to(outside, target_is_directory=True)
    manager = WorkspaceManager(config)
    monkeypatch.setattr(manager, "_validate_source", lambda: None)

    with pytest.raises(WorkspaceError, match="worktree path is unsafe"):
        manager.prepare(issue_number=20, title="Improve Eve tracing")

    assert list(outside.iterdir()) == []


def test_worktree_branch_cannot_add_path_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    manager = WorkspaceManager(HaloConfig.load(tmp_path))
    monkeypatch.setattr(manager, "_validate_source", lambda: None)

    with pytest.raises(WorkspaceError, match="escapes its managed root"):
        manager.prepare(
            issue_number=20,
            title="Ignored",
            persisted_branch="halo/issue-20-safe/nested",
        )
