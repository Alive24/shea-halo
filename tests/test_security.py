from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shea_halo.security import (
    HOST_PATH_REDACTED,
    REDACTED,
    PersistenceSafetyError,
    contains_host_absolute_path,
    contains_sensitive_text,
    git_environment,
    is_sensitive_path,
    redact_data,
    redact_host_paths,
    redact_text,
    sanitize_persisted_data,
    sanitize_persisted_text,
    sanitized_environment,
)
from shea_halo.workspace import WorkspaceError, WorkspaceManager


def test_sanitized_environment_removes_credentials_and_preserves_safe_values() -> None:
    environment = {
        "PATH": "/usr/bin",
        "LANG": "en_GB.UTF-8",
        "OPENAI_API_KEY": "sk-environment-secret",
        "GH_TOKEN": "ghp_environmentsecret",
        "SERVICE_PASSWORD": "password-value",
        "DATABASE_URL": "postgres://user:password@example.test/db",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }

    sanitized = sanitized_environment(environment)

    assert sanitized == {"PATH": "/usr/bin", "LANG": "en_GB.UTF-8"}


def test_git_environment_preserves_handles_but_not_raw_credentials() -> None:
    environment = {
        "PATH": "/usr/bin",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GIT_ASKPASS": "/usr/local/bin/git-askpass",
        "SSH_ASKPASS": "/usr/local/bin/ssh-askpass",
        "GH_TOKEN": "ghp_environmentsecret",
        "OPENAI_API_KEY": "sk-environment-secret",
        "GIT_PASSWORD": "password-value",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/tmp/untrusted-hooks",
        "GIT_DIR": "/tmp/untrusted-repository",
        "GIT_COMMITTER_NAME": "Untrusted Committer",
        "GIT_NO_REPLACE_OBJECTS": "0",
    }

    sanitized = git_environment(environment)

    assert sanitized == {
        "PATH": "/usr/bin",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "GIT_ASKPASS": "/usr/local/bin/git-askpass",
        "SSH_ASKPASS": "/usr/local/bin/ssh-askpass",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def test_redact_text_removes_known_and_common_credential_forms() -> None:
    known = "known-environment-secret"
    text = "\n".join(
        [
            known,
            "sk-abcdefghijklmnopqrstuvwxyz",
            "github_pat_abcdefghijklmnopqrstuvwxyz",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "Cookie: sessionid=supersecretvalue",
            "-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----",
            "https://alice:supersecretvalue@example.test/private",
            'api_key = "literal-secret-value"',
            "password: another-secret-value",
        ]
    )

    redacted = redact_text(text, {"OPENAI_API_KEY": known})

    assert known not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "literal-secret-value" not in redacted
    assert "another-secret-value" not in redacted
    assert "dXNlcjpwYXNzd29yZA" not in redacted
    assert "sessionid" not in redacted
    assert "private-key-material" not in redacted
    assert "alice:supersecretvalue" not in redacted
    assert redacted.count(REDACTED) >= 11


def test_structured_redaction_preserves_valid_json() -> None:
    payload = {
        "token": None,
        "nested": {
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
            "message": "Bearer abcdefghijklmnopqrstuvwxyz",
        },
    }

    rendered = json.dumps(redact_data(payload, {}))
    parsed = json.loads(rendered)

    assert parsed["token"] == REDACTED
    assert parsed["nested"]["api_key"] == REDACTED
    assert parsed["nested"]["message"] == REDACTED


def test_large_benign_text_is_scanned_without_assignment_regex_pathology() -> None:
    text = "x" * 100_000

    assert not contains_sensitive_text(text, environment={})
    assert sanitize_persisted_text(text, environment={}) == text


def test_persisted_text_uses_logical_roots_and_removes_other_host_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "target" / ".shea" / "worktrees" / "halo" / "issue-26"
    text = "\n".join(
        [
            f"workspace source: {workspace / 'src/agent.py'}",
            "external config: /Users/alice/.config/halo/config.json",
            "CI workspace: /data/jenkins/workspace/repo/file.py",
            "file URI: file:///Users/alice/private/config.json",
            r"UNC path: \\server\share\secret\file.txt",
            "documentation: https://docs.inference.net/integrations/traces/eve",
            "collector route: /v1/traces",
        ]
    )

    sanitized = sanitize_persisted_text(
        text,
        path_labels={workspace: "<workspace>"},
        environment={},
    )

    assert "<workspace>/src/agent.py" in sanitized
    assert HOST_PATH_REDACTED in sanitized
    assert str(tmp_path) not in sanitized
    assert "/Users/alice" not in sanitized
    assert "/data/jenkins" not in sanitized
    assert "file://" not in sanitized
    assert "server" not in sanitized
    assert "https://docs.inference.net/integrations/traces/eve" in sanitized
    assert "/v1/traces" in sanitized
    assert not contains_host_absolute_path(sanitized)


def test_structured_persistence_sanitizes_keys_values_and_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    secret = "sk-persisted-credential-value"
    payload = {
        "token": secret,
        f"source:{workspace / 'agent.py'}": {
            "message": f"read {workspace / 'src/main.py'} with {secret}",
        },
    }

    sanitized = sanitize_persisted_data(
        payload,
        path_labels={workspace: "<workspace>"},
        environment={"OPENAI_API_KEY": secret},
    )
    rendered = json.dumps(sanitized)

    assert secret not in rendered
    assert str(tmp_path) not in rendered
    assert sanitized["token"] == REDACTED
    assert "source:<workspace>/agent.py" in sanitized
    assert "<workspace>/src/main.py" in rendered


def test_redact_host_paths_keeps_repository_relative_paths() -> None:
    text = "\n".join(
        [
            "Inspect src/shea_halo/agent.py and docs/tracing.md.",
            "#!/usr/bin/env node",
            'const endpoint = base.replace(/\\/$/, "") + "/v1/traces";',
            (
                "integrity: sha512-4+/OFSqOjoyULo7eN7EA97DE0Xydj/"
                "PW5aIckxqQIoFjFwqXKuFCvXUJObyJfBF9Khu4RL/"
                "jlDRI9FPaMGfPnw=="
            ),
            "",
        ]
    )

    assert redact_host_paths(text) == text
    assert not contains_host_absolute_path(text)


def test_host_path_detection_rejects_machine_specific_shebang() -> None:
    text = "#!/Users/alice/project/.venv/bin/python\nprint('unsafe')\n"

    assert contains_host_absolute_path(text)
    assert redact_host_paths(text).startswith(f"#!{HOST_PATH_REDACTED}\n")


def test_structured_persistence_fails_closed_on_redacted_key_collision() -> None:
    with pytest.raises(PersistenceSafetyError, match="duplicate keys"):
        sanitize_persisted_data(
            {
                "/Users/alice/repo": "first",
                "/Users/bob/repo": "second",
            },
            environment={},
        )


def test_secret_detection_allows_runtime_credential_references() -> None:
    source = "\n".join(
        [
            'api_key = os.getenv("OPENAI_API_KEY")',
            "auth = validate_request(request)",
            "token = request.token",
        ]
    )

    assert not contains_sensitive_text(source, {})
    assert contains_sensitive_text('api_key = "literal-production-secret"', {})
    assert contains_sensitive_text("token=abc-123-secret-value", {})
    assert contains_sensitive_text("Authorization: Basic dXNlcjpwYXNzd29yZA==", {})
    assert contains_sensitive_text("Cookie: sessionid=supersecretvalue", {})
    assert contains_sensitive_text(
        "-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----",
        {},
    )


def test_secret_detection_does_not_cross_dependency_mapping_lines() -> None:
    lockfile = """\
packages:
  idb-keyval:
    optional: true
  token-types: 6.1.2
  path-key: 3.1.1
  cookie: 0.7.2
"""

    assert not contains_sensitive_text(lockfile, {})


@pytest.mark.parametrize(
    "path",
    [
        ".env.local",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "credentials.json",
        ".aws/credentials",
        "deploy/client-secret.yaml",
        "service_credentials.toml",
        "keys/id_ed25519",
        "keys/private.pem",
    ],
)
def test_sensitive_path_recognizes_credential_files(path: str) -> None:
    assert is_sensitive_path(Path(path))


@pytest.mark.parametrize(
    "path",
    [
        "src/secret_scanner.py",
        "tests/test_credentials.py",
        "docs/credentials.md",
        "src/authentication.ts",
    ],
)
def test_sensitive_path_allows_source_and_documentation(path: str) -> None:
    assert not is_sensitive_path(Path(path))


def test_workspace_subprocess_sanitizes_environment_and_redacts_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-workspace-environment-secret"
    captured_environment: dict[str, str] = {}
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("SAFE_SETTING", "preserved")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        text: bool,
        capture_output: bool,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, text, capture_output, check
        captured_environment.update(env)
        return subprocess.CompletedProcess(args, 1, "", f"failed with {secret}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(WorkspaceError) as error:
        WorkspaceManager._run(tmp_path, ["git", "status"])

    assert "OPENAI_API_KEY" not in captured_environment
    assert captured_environment["SAFE_SETTING"] == "preserved"
    assert captured_environment["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert secret not in str(error.value)
    assert REDACTED in str(error.value)
