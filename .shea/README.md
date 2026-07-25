# Shea Halo Shea runtime

This directory is the committed Shea Symphony configuration for Shea Halo. It is initialized from the proven FailureReport runtime layout and then bound to Shea Halo's repository, Project, prompts, and Python verification contract.

Tracked configuration:

- app-profile.json selects the shared workflow.
- workflows/shea-symphony.md contains the GitHub Project #11 and lane runtime configuration.
- prompts/ contains the separate Main, Review, and Merge lane contracts.
- template/ contains the shared append-only timeline and workpad rendering contracts.

`workflows/`, `prompts/`, and `template/` are Shea Halo-owned integration contracts and may be tailored to this Python repository. `.shea/app/` and `.shea/bin/` are copied unchanged from the vendored Shea Symphony 2606 MVP runtime; do not patch them when changing Shea Halo's tracker policy or lane instructions.

Machine-local files are ignored by the repository root .gitignore:

- logs/, artifacts/, and worktrees/ are runtime output.
- any file whose name contains .local. under .shea/ is a machine-specific override, for example workflows/shea-symphony.local.md or app-profile.local.json.

The 2606 MVP workflow loader does not merge a base workflow with a .local workflow automatically. A local workflow must therefore be a complete valid workflow, and a local profile must explicitly select it. For example, create the ignored .shea/app-profile.local.json with:

    {
      "workflow_path": ".shea/workflows/shea-symphony.local.md"
    }

Run the Tauri app from an independent Shea Symphony 2606 MVP checkout's app directory, selecting this target profile explicitly:

    SHEA_SYMPHONY_APP_PROFILE_PATH="$PWD/.shea/app-profile.json" \
      npm run tauri -- dev

The shared profile includes the copied target-local CLI path for CLI workflows. The Tauri app may still use its 2606 engine bridge when launched from an independent Shea Symphony checkout. Do not use a browser-only development server to orchestrate Shea Halo.

260720 Notes: We are using vendored version of Shea Symphony 2606 by running:

SHEA_SYMPHONY_APP_PROFILE_PATH="$PWD/.shea/app-profile.json" ./.shea/app/shea-symphony-app

Repository verification is Python-native:

    uv sync --locked --all-groups
    uv run ruff format --check .
    uv run ruff check .
    uv run basedpyright
    uv run pytest
    uv build
