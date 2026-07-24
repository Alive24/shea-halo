from __future__ import annotations

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
    monkeypatch.setattr(manager, "_validate_source", lambda: None)

    workspace = manager.prepare(
        issue_number=20,
        title="Improve Eve tracing",
    )
    run(workspace.path, "git", "config", "user.name", "Halo Test")
    run(workspace.path, "git", "config", "user.email", "halo@example.test")
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


def test_publication_rejects_clean_filter_changed_after_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, manager, workspace = _prepared_workspace(tmp_path, monkeypatch)
    (workspace.path / ".gitattributes").write_text(
        "*.txt filter=halo-test\n",
        encoding="utf-8",
    )
    candidate = workspace.path / "instrumentation.txt"
    candidate.write_text("original\n", encoding="utf-8")
    run(workspace.path, "git", "config", "filter.halo-test.clean", "cat")
    run(workspace.path, "git", "config", "filter.halo-test.smudge", "cat")
    intent = manager.create_publication_intent(workspace, expected_snapshot=None)
    assert intent is not None
    hook = Path(run(workspace.path, "git", "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = workspace.path / hook
    hook.write_text(
        "#!/bin/sh\n"
        "git config filter.halo-test.clean 'sed s/original/tampered/'\n"
        "git add instrumentation.txt\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with pytest.raises(WorkspaceError, match="publication intent"):
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
    monkeypatch.setattr(manager, "_validate_source", lambda: None)
    workspace = manager.prepare(issue_number=20, title="Improve Eve tracing")
    run(workspace.path, "git", "config", "user.name", "Halo Test")
    run(workspace.path, "git", "config", "user.email", "halo@example.test")
    return remote, manager, workspace


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
