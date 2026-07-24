from __future__ import annotations

import subprocess
from pathlib import Path


def run(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    return process.stdout.strip()


def write_target_config(root: Path, repository: str = "Alive24/FailureReport") -> None:
    config = root / ".shea" / "halo.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""\
schema_version = "shea-halo/v1"

[target]
repository = "{repository}"
base_branch = "main"

[tracker]
owner = "Alive24"
owner_type = "user"
project_number = 10

[observation]
trace_globs = [".shea/artifacts/halo/traces/*.jsonl"]

[setup]
commands = [["python", "-m", "pip", "--version"]]

[experiments]
commands = [["python", "-m", "pytest"]]

[verification]
commands = [["python", "-m", "pytest"]]
""",
        encoding="utf-8",
    )
