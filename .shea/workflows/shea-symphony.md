---
tracker:
  kind: github_project_v2
  owner: Alive24
  repo: shea-halo
  project_owner: Alive24
  project_owner_type: user
  project_number: 11
  status_field: Status
  state_map:
    backlog: Backlog
    todo: Todo
    need_to_clarify: Need to Clarify
    in_progress: In Progress
    need_human_input: Need Human Input
    agent_review: Agent Review
    human_review: Human Review
    rework: Rework
    merging: Merging
    done: Done
  active_states:
    - Todo
    - Rework
  terminal_states:
    - Done
    - Dogfood
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
  assignee_filter:
    source: issue_assignees
    additional_assignees: []
  workpad:
    source: issue_comment
    marker: "<!-- shea-symphony-workpad -->"

git:
  base_branch: main

prompts:
  main_agent: ../prompts/main-agent.md
  review_agent: ../prompts/review-agent.md
  merge_agent: ../prompts/merge-agent.md
backend_prompts:
  codex_app_server: ../prompts/backend/codex-app-server.md
  automatic_review: ../prompts/backend/automatic-review.md
  automatic_review_structured: ../prompts/backend/automatic-review-structured.md
  claude_code_review: ../prompts/backend/claude-code-review.md
  merge_repair: ../prompts/backend/merge-repair.md
workpad_templates:
  agent_review_run: ../template/workpad/agent-review.md
  doctor_triage: ../template/workpad/doctor-triage.md
  human_review_repair: ../template/workpad/doctor-triage.md
  merge_run: ../template/workpad/merge-run.md
  merge_repair: ../template/workpad/merge-run.md
  forge_rework_run: ../template/workpad/rework-run.md
  forge_rework_blocked: ../template/workpad/rework-run.md

polling:
  interval_ms: 50000

artifacts:
  root: ../artifacts
  namespace: Alive24/shea-halo

workspace:
  root: ../worktrees

main_lane:
  backend: codex
  max_concurrent_agents: 3
  max_turns: 3
  max_retry_backoff_ms: 300000

tmux:
  command: tmux
  agent_command: codex
  review_agent_command: agy
  session_prefix: shea-halo

codex:
  command: codex app-server -c 'service_tier="fast"'
  reasoning_effort: high
  approval_policy: never
  stall_timeout_ms: 300000
  session_stale_after_ms: 1800000

review_lane:
  backend: agy-cli
  agy_command: agy
  agy_model: "Gemini 3.1 Pro (High)"
  timeout_ms: 1200000
  max_concurrent_workers: 3

merge_lane:
  agent_backend: codex
  max_concurrent_workers: 3

quality_gate:
  llm:
    mode: disabled

verification:
  timeout_ms: 600000
  commands:
    - uv sync --locked --all-groups
    - uv run ruff format --check .
    - uv run ruff check .
    - uv run basedpyright
    - uv run pytest
    - uv build

observability:
  logs_root: ../logs
---

# Shea Halo Shea Symphony workflow

This is the committed Shea Symphony runtime definition for the Shea Halo repository and GitHub Project v2 #11. Lane behavior lives in the adjacent prompt contracts so implementation, review, and merge authority stay separate.

The runtime state directories are deliberately relative to this workflow:

- worktrees: .shea/worktrees
- artifacts: .shea/artifacts
- logs: .shea/logs

They are machine-local and ignored by Git.
