# NextResearcher Agent Guide

## Table of Contents
- [Project Purpose](#project-purpose)
- [Start Here](#start-here)
- [Repository Map](#repository-map)
- [Core Workflows](#core-workflows)
- [Generated Runtime State](#generated-runtime-state)
- [Configuration Notes](#configuration-notes)
- [Coding Contracts](#coding-contracts)

## Project Purpose
NextResearcher is an automated experiment orchestrator for coding agents. It starts Codex app-server sessions in isolated git worktrees, lets an agent improve a target repository against a measurable objective, runs hidden evaluations on snapshots, promotes the best result, and writes logs plus learning artifacts for later runs.

Keep changes general-purpose. This project should help optimize any target repo and objective; avoid assumptions tied to one problem type, language, framework, file extension, or benchmark unless the user explicitly requests that scope.

## Start Here
| Path | Purpose |
| --- | --- |
| `Main.py` | Runtime entrypoint. Ensures default prompt files exist, loads or creates evaluator setup, then starts the experiment loop. |
| `CodexConfig.example.toml` | Committed starter template for local experiment configuration. |
| `CodexConfig.toml` | Ignored local experiment configuration. Defines the target repo, eval command, score direction, iteration count, Codex role, eval repo, and override files. |
| `Orchestrator/ExperimentRunner.py` | Main experiment loop. Creates worktrees, runs baseline and trial evaluations, records results, promotes the best commit, and triggers reflection. |
| `Orchestrator/ExperimentSession.py` | Per-iteration Codex session protocol. Handles the hidden eval tool, completion marker, and summary turn. |
| `Agents/Codex/SessionRunner.py` | High-level Codex session wrapper. Loads role prompts and starts `CodexAgent` with a clean environment. |
| `Agents/Codex/Agent.py` | Low-level JSON-RPC client for `codex app-server`. Handles threads, turns, approvals, dynamic tool calls, streamed messages, command logs, and file-change logs. |

## Repository Map
| Path | Responsibility |
| --- | --- |
| `Agents/` | Agent integrations. Currently contains the Codex app-server integration. |
| `Agents/Codex/Agent.py` | Process management and JSON-RPC protocol handling for `codex app-server`. |
| `Agents/Codex/SessionRunner.py` | Role prompt loading, environment cleanup, and context-manager API for Codex sessions. |
| `Agents/Codex/SessionLog.py` | Markdown session logs for user requests, responses, commands, tool calls, errors, and recoveries. |
| `Orchestrator/` | Experiment orchestration package. Imports `ensure_evaluator_setup` and `run_experiment_loop` through `Orchestrator/__init__.py`. |
| `Orchestrator/Setup/` | Evaluator setup and validation. Can launch a setup Codex session, ask user clarification, generate evaluator artifacts, and write `CodexConfig.toml`. |
| `Orchestrator/Evaluation/` | Hidden evaluation support. Defines `run_hidden_eval`, score parsing, eval command execution, eval override copying, prewarm handling, and eval worktree syncing. |
| `Orchestrator/State/` | Git state helpers. Creates/removes worktrees, snapshots trials, manages experiment branches, and maintains `best/current` plus `BestState.json`. |
| `Orchestrator/Artifacts/` | Structured result and log writers for experiment runs and iteration JSONL records. |
| `Orchestrator/Learning/` | Iteration summary parsing, run reflection, experiment memory validation, and durable learning updates. |
| `Prompts/` | Role instructions injected into Codex sessions. `Base.md` is always loaded; role-specific prompts are loaded by role name. |
| `ConfigGuard.py` | Creates default prompt files and bootstraps missing local config from `CodexConfig.example.toml`. |
| `ResetExperiments.py` | Local reset utility. Removes generated worktrees, generated evaluator artifacts, logs, best-state metadata, experiment memory, and experiment branches for the configured target repo. |
| `ExampleUsage/` | Small example for direct `CodexSession` use. |
| `Documentations/` | Reference documentation, currently for Codex app-server. |
| `pyproject.toml` / `uv.lock` | Python project metadata and locked dependencies. The project targets Python 3.13 and uses `uv`. |

## Core Workflows
### Evaluator Setup
1. `Main.py` calls `ensure_project_files()` to create missing default prompts and bootstrap missing local config.
2. `ensure_evaluator_setup()` reads `CodexConfig.toml`.
3. If config is missing or invalid, setup launches a Codex session with the `eval_setup` role.
4. The setup agent may ask focused user clarification through `ask_user_clarification`.
5. Generated evaluator files must stay inside `GeneratedEvals/<target_repo_name>/`.
6. The setup agent submits config through `submit_eval_setup`.
7. Validation creates a worktree, applies eval overrides, optionally runs prewarm, runs `eval_command`, and requires the final non-empty stdout line to parse as a number.

### Experiment Loop
1. `run_experiment_loop()` loads the existing best state or starts from target repo `HEAD`.
2. Each iteration creates two detached worktrees: one for the agent and one for evaluation.
3. Baseline evaluation runs before the agent session.
4. `run_iteration_session()` starts a Codex session in the agent worktree with the configured role.
5. The agent may request hidden evaluation through `run_hidden_eval`.
6. Each eval request commits a snapshot in the agent worktree, syncs the eval worktree to that commit, runs the eval command, and returns score feedback on the next turn.
7. The agent must finish with a standalone `EXPERIMENT_COMPLETE` marker.
8. A summary turn produces structured JSON for logs and learning.

### Best State Promotion
- Trial snapshots are normal commits created in the agent worktree.
- The best trial in each iteration is saved temporarily as `experiment/iter_NNN`.
- If the best trial improves the current best score, `best/current` is moved to that commit and `BestState.json` is updated.
- `load_best_state()` requires `best/current` and `BestState.json` to agree. If state is inconsistent, reset experiments before continuing.

### Reflection And Memory
- Iteration records are appended to `Logs/iteration_records_<run_id>.jsonl`.
- `run_reflection()` selects useful session logs and asks a reflection Codex session to produce a run reflection plus updated experiment memory.
- `Prompts/ExperimentMemory.md` is generated runtime state and must remain problem-agnostic and language-agnostic.

## Generated Runtime State
These paths are generated and ignored by git:
- `Logs/`: experiment logs, iteration records, Codex session logs, and run reflections.
- `Worktrees/`: temporary agent, eval, and validation worktrees.
- `GeneratedEvals/`: generated evaluator artifacts for target repos.
- `Prompts/ExperimentMemory.md`: learned heuristics from prior experiment runs.
- `BestState.json`: metadata for the promoted best state.

Do not treat generated artifacts as source unless the user explicitly asks to inspect or preserve them.

## Configuration Notes
- Use `uv` for project execution and dependency management.
- `CodexConfig.toml` is local runtime configuration and is ignored by git.
- If `CodexConfig.toml` is missing, startup copies `CodexConfig.example.toml` to create it.
- `eval_strategy` must be exactly `maximize` or `minimize`.
- `eval_command` runs from the eval worktree and may use `{worktree}` or `{eval_worktree}` placeholders.
- Evaluators must print the numeric score as the final non-empty stdout line.
- `eval_overrides` are paths or glob patterns relative to `eval_repo`; absolute paths and `..` are invalid.
- `prewarm_command` is optional. If used with `prewarm_watch_files`, the eval worktree is rewarmed only when watched files change.

## Coding Contracts specific for this codebase

### 1) Keep Changes Problem-Agnostic and Language-Agnostic
- Do not design changes, proposals, prompts, workflows, schemas, or logic around a single problem type unless the user explicitly requests that scope.
- Do not make implementations depend on a specific programming language, framework, ecosystem, file extension, or toolchain unless that dependency is explicitly required.
- Default to general mechanisms that can work across different problem domains and across codebases written in different languages.
- If an approach would make NextResearcher biased toward one category of task or one language, stop and ask the user before implementing it.
- Avoid hardcoded assumptions such as:
  - logic that only applies to one problem type
  - prompts or pipelines tailored only to Python, JavaScript, or any other single language
  - language-specific heuristics presented as universal behavior
- When proposing or implementing changes, preserve NextResearcher's role as a general-purpose system that should work on any type of problem regardless of programming language.

### 2) Tests
- No test file is required by default.
- If verification is needed, prefer the narrowest command that checks the changed behavior.
