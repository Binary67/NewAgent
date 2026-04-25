from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from Agents.Codex import CodexSession

from .BestState import BEST_BRANCH, load_best_state, promote_best_state
from .Evaluation import (
    HIDDEN_EVAL_TOOL,
    apply_eval_overrides,
    build_eval_followup_message,
    build_eval_handler,
    get_prewarm_watch_state,
    is_better,
    parse_score,
    run_eval,
    run_prewarm_command,
    run_requested_eval,
)
from .ExperimentLog import append_iteration, append_summary, write_header
from .Learning import (
    build_reflection_request,
    build_summary_request,
    choose_reflection_logs,
    ensure_default_experiment_memory,
    is_experiment_complete,
    list_changed_files,
    parse_experiment_summary,
    parse_reflection_response,
    append_iteration_record,
)
from .Workspace import create_worktree, delete_branches, get_head_commit

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_experiment_loop(
    target_repo: str | Path,
    eval_command: str,
    role: str = "experiment",
    num_iterations: int = 5,
    max_eval_calls: int = 3,
    eval_strategy: str = "maximize",
    eval_repo: str | Path = "",
    eval_overrides: list[str] | None = None,
    prewarm_command: str = "",
    prewarm_watch_files: list[str] | None = None,
):
    maximize = eval_strategy == "maximize"
    target_repo = Path(target_repo).resolve()
    eval_repo_path = Path(eval_repo).resolve() if eval_repo else None
    eval_overrides = eval_overrides or []
    prewarm_watch_files = prewarm_watch_files or []
    worktree_dir = PROJECT_ROOT / "Worktrees"
    logs_dir = PROJECT_ROOT / "Logs"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        current_best_commit, current_best_score = load_best_state(target_repo, eval_strategy)
    except RuntimeError as exc:
        print(exc)
        return []

    if current_best_commit:
        print(f"Resuming from {BEST_BRANCH}: {current_best_commit} (score: {current_best_score})")
    else:
        current_best_commit = get_head_commit(target_repo)
        print(f"Starting from HEAD: {current_best_commit}")

    run_id = f"{datetime.now():%Y%m%d_%H%M%S}"
    initial_commit = current_best_commit
    experiment_log = logs_dir / f"experiment_{run_id}.md"
    iteration_record_path = logs_dir / f"iteration_records_{run_id}.jsonl"
    run_reflection_path = logs_dir / f"run_reflection_{run_id}.md"
    experiment_memory_path = PROJECT_ROOT / "Prompts" / "ExperimentMemory.md"
    ensure_default_experiment_memory(experiment_memory_path)
    write_header(experiment_log, target_repo, initial_commit, eval_command, num_iterations, eval_strategy)

    results = []
    fatal_error = ""

    for iteration in range(1, num_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"  Iteration {iteration} / {num_iterations}")
        print(f"{'=' * 60}")

        base_commit = current_best_commit
        agent_worktree = worktree_dir / f"iteration_{iteration:03d}_agent"
        eval_worktree = worktree_dir / f"iteration_{iteration:03d}_eval"

        try:
            create_worktree(target_repo, agent_worktree, base_commit)
            create_worktree(target_repo, eval_worktree, base_commit)
        except subprocess.CalledProcessError as exc:
            print(f"Worktree creation failed: {exc}")
            result = _make_result(
                iteration,
                agent_worktree,
                base_commit=base_commit,
                status="worktree_error",
                error=str(exc),
            )
            results.append(result)
            append_iteration(experiment_log, result, BEST_BRANCH)
            append_iteration_record(iteration_record_path, _build_iteration_record(run_id, result))
            continue

        if eval_repo_path:
            apply_eval_overrides(eval_repo_path, eval_worktree, eval_overrides)

        print(f"Agent worktree ready: {agent_worktree}")
        print(f"Eval worktree ready:  {eval_worktree}")

        if prewarm_command:
            agent_prewarm_ok, agent_prewarm_error = run_prewarm_command(
                agent_worktree,
                prewarm_command,
                action="Prewarming agent worktree",
            )
            if not agent_prewarm_ok:
                print(agent_prewarm_error)
                result = _make_result(
                    iteration,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=agent_prewarm_error,
                )
                results.append(result)
                append_iteration(experiment_log, result, BEST_BRANCH)
                append_iteration_record(iteration_record_path, _build_iteration_record(run_id, result))
                continue

            eval_prewarm_ok, eval_prewarm_error = run_prewarm_command(
                eval_worktree,
                prewarm_command,
                action="Prewarming eval worktree",
            )
            if not eval_prewarm_ok:
                print(eval_prewarm_error)
                result = _make_result(
                    iteration,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=eval_prewarm_error,
                )
                results.append(result)
                append_iteration(experiment_log, result, BEST_BRANCH)
                append_iteration_record(iteration_record_path, _build_iteration_record(run_id, result))
                continue

        eval_prewarm_state = get_prewarm_watch_state(eval_worktree, prewarm_watch_files)

        baseline_stdout, baseline_error = run_eval(eval_command, eval_worktree)
        baseline_score = parse_score(baseline_stdout) if not baseline_error else None
        if baseline_score is not None:
            print(f"Baseline score: {baseline_score}")
        else:
            print(f"Baseline eval failed: {baseline_error or 'unparseable output'}")

        eval_state: dict[str, Any] = {
            "remaining": max_eval_calls,
            "baseline_score": baseline_score,
            "trials": [],
            "prewarm_state": eval_prewarm_state,
            "pending_request": None,
            "requested_this_turn": False,
        }
        eval_handler = build_eval_handler(agent_worktree, eval_state)

        instruction = (
            f"IMPORTANT: You must only create or modify files within your current "
            f"working directory ({agent_worktree}). "
            f"Do not access, read, or modify any files outside this directory."
        )

        codex_response = ""
        codex_failed = False
        protocol_error = ""
        session_log = None
        iteration_summary: dict[str, object] | None = None
        start_time = time.time()
        try:
            with CodexSession(
                cwd=agent_worktree,
                role=role,
                dynamic_tools=[HIDDEN_EVAL_TOOL],
                tool_handler=eval_handler,
            ) as session:
                session_log = session.session_log_path
                turn_input = instruction
                while True:
                    eval_state["pending_request"] = None
                    eval_state["requested_this_turn"] = False
                    turn_result = session.run_turn(turn_input)
                    codex_response = turn_result.response_text
                    session_log = session.session_log_path

                    pending_request = eval_state["pending_request"]
                    if pending_request is None:
                        if not is_experiment_complete(codex_response):
                            raise ValueError(
                                "Experiment turn ended without a standalone EXPERIMENT_COMPLETE marker."
                            )

                        current_iteration_best_score = None
                        if eval_state["trials"]:
                            current_iteration_best_score = (max if maximize else min)(
                                eval_state["trials"],
                                key=lambda trial: trial["score"],
                            )["score"]

                        summary_prompt = build_summary_request(
                            iteration,
                            baseline_score,
                            current_iteration_best_score,
                            eval_state["remaining"],
                        )
                        eval_state["pending_request"] = None
                        eval_state["requested_this_turn"] = False
                        summary_turn = session.run_turn(summary_prompt)
                        if eval_state["pending_request"] is not None:
                            raise ValueError(
                                "Summary turn must not request hidden evaluation."
                            )
                        if summary_turn.commands or summary_turn.file_changes:
                            raise ValueError(
                                "Summary turn must not run commands or modify files."
                            )
                        iteration_summary = parse_experiment_summary(summary_turn.response_text)
                        break

                    eval_feedback = run_requested_eval(
                        eval_command,
                        eval_worktree,
                        eval_repo_path,
                        eval_overrides,
                        prewarm_command,
                        prewarm_watch_files,
                        eval_state,
                        pending_request,
                        maximize,
                    )
                    turn_input = build_eval_followup_message(pending_request["commit"], eval_feedback)
            print(f"Codex done. Session log: {session_log}")
        except ValueError as exc:
            protocol_error = str(exc)
            print(f"Protocol error: {exc}")
        except Exception as exc:
            codex_failed = True
            codex_response = str(exc)
            print(f"Codex failed: {exc}")
        codex_duration = round(time.time() - start_time, 1)

        trials = eval_state["trials"]
        best_trial = None
        if trials:
            best_trial = (max if maximize else min)(trials, key=lambda trial: trial["score"])

        promotion_error = ""
        best_score_str = str(best_trial["score"]) if best_trial else ""
        if protocol_error:
            status = "protocol_error"
        elif codex_failed:
            status = "codex_error"
        else:
            status = "completed"

        result = _make_result(
            iteration,
            agent_worktree,
            base_commit=base_commit,
            session_log=str(session_log) if session_log else None,
            codex_response=codex_response,
            eval_score=best_score_str,
            codex_duration_s=codex_duration,
            status=status,
            error=protocol_error or ("" if not codex_failed else codex_response),
            baseline_score=baseline_score,
            eval_calls_used=len(trials),
            summary=iteration_summary,
            trials=trials,
            promoted_to_best=False,
        )

        if best_trial:
            result["commit_hash"] = best_trial["commit"]
            result["parsed_score"] = best_trial["score"]
            try:
                result["files_changed_best_trial"] = list_changed_files(
                    target_repo,
                    base_commit,
                    best_trial["commit"],
                )
            except Exception as exc:
                print(f"Warning: failed to list changed files for iteration {iteration}: {exc}")
                result["files_changed_best_trial"] = []
            try:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target_repo),
                        "branch",
                        "-f",
                        f"experiment/iter_{iteration:03d}",
                        best_trial["commit"],
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                print(f"Saved best trial to branch: experiment/iter_{iteration:03d} (score: {best_trial['score']})")
            except Exception as exc:
                print(f"Warning: failed to save branch for iteration {iteration}: {exc}")

            should_promote = current_best_score is None or is_better(best_trial["score"], current_best_score, maximize)
            if should_promote:
                try:
                    promote_best_state(target_repo, best_trial["commit"], best_trial["score"], eval_strategy)
                    current_best_commit = best_trial["commit"]
                    current_best_score = best_trial["score"]
                    result["promoted_to_best"] = True
                    print(f"Updated {BEST_BRANCH}: {current_best_commit} (score: {current_best_score})")
                except Exception as exc:
                    promotion_error = f"Failed to update best state: {exc}"
                    result["status"] = "best_state_error"
                    result["error"] = promotion_error
                    print(promotion_error)

        results.append(result)
        append_iteration(experiment_log, result, BEST_BRANCH)
        append_iteration_record(iteration_record_path, _build_iteration_record(run_id, result))

        if promotion_error:
            fatal_error = promotion_error
            break

    delete_branches(target_repo, "experiment/iter_*")

    completed_results = [result for result in results if result.get("parsed_score") is not None]
    best_result = None

    if completed_results:
        best_result = (max if maximize else min)(completed_results, key=lambda result: result["parsed_score"])
    elif not fatal_error:
        print("No successful iterations. Keeping existing best state (if any).")

    append_summary(experiment_log, results, best_result, fatal_error=fatal_error)
    _run_reflection(
        results,
        maximize,
        run_id,
        iteration_record_path,
        run_reflection_path,
        experiment_memory_path,
    )

    if fatal_error:
        print(f"\nExperiment stopped early. Log: {experiment_log}")
    else:
        print(f"\nExperiment complete. Log: {experiment_log}")
    return results


def _make_result(iteration, worktree_path, **kwargs):
    return {
        "iteration": iteration,
        "worktree": str(worktree_path),
        "base_commit": "",
        "session_log": None,
        "codex_response": "",
        "eval_score": "",
        "baseline_score": None,
        "eval_calls_used": 0,
        "codex_duration_s": 0,
        "status": "completed",
        "error": "",
        "promoted_to_best": False,
        "summary": None,
        "files_changed_best_trial": [],
        **kwargs,
    }


def _build_iteration_record(run_id: str, result: dict[str, object]) -> dict[str, object]:
    baseline_score = result.get("baseline_score")
    best_score = result.get("parsed_score")
    score_delta = None
    if isinstance(baseline_score, (int, float)) and isinstance(best_score, (int, float)):
        score_delta = float(best_score) - float(baseline_score)

    changed_files = result.get("files_changed_best_trial")
    if not isinstance(changed_files, list):
        changed_files = []

    return {
        "run_id": run_id,
        "iteration": result["iteration"],
        "status": result["status"],
        "base_commit": result.get("base_commit") or "",
        "best_trial_commit": result.get("commit_hash"),
        "baseline_score": baseline_score,
        "best_score": best_score,
        "score_delta": score_delta,
        "eval_calls_used": result.get("eval_calls_used", 0),
        "promoted_to_best": result.get("promoted_to_best", False),
        "files_changed_best_trial": [str(path) for path in changed_files if isinstance(path, str)],
        "session_log": result.get("session_log"),
        "summary": result.get("summary"),
    }


def _run_reflection(
    results: list[dict[str, object]],
    maximize: bool,
    run_id: str,
    iteration_record_path: Path,
    run_reflection_path: Path,
    experiment_memory_path: Path,
) -> None:
    if not iteration_record_path.exists():
        run_reflection_path.write_text(
            _build_reflection_fallback("No iteration records were produced. Experiment memory left unchanged."),
            encoding="utf-8",
        )
        return

    selected_logs = choose_reflection_logs(results, maximize)
    reflection_request = build_reflection_request(
        run_id,
        iteration_record_path,
        experiment_memory_path,
        selected_logs,
    )

    try:
        with CodexSession(cwd=PROJECT_ROOT, role="reflection") as session:
            reflection_turn = session.run_turn(reflection_request)
        run_reflection, experiment_memory = parse_reflection_response(reflection_turn.response_text)
    except Exception as exc:
        print(f"Reflection failed: {exc}")
        run_reflection_path.write_text(
            _build_reflection_fallback(f"Reflection failed: {exc}. Experiment memory left unchanged."),
            encoding="utf-8",
        )
        return

    run_reflection_path.write_text(f"{run_reflection.rstrip()}\n", encoding="utf-8")
    experiment_memory_path.write_text(f"{experiment_memory.rstrip()}\n", encoding="utf-8")
    print(f"Reflection complete. Run reflection: {run_reflection_path}")


def _build_reflection_fallback(message: str) -> str:
    return (
        "# Run Reflection\n\n"
        "## Patterns That Helped\n"
        "- No reflection output was available.\n\n"
        "## Patterns That Hurt\n"
        f"- {message}\n\n"
        "## Unresolved Questions\n"
        "- Reflection artifacts should be reviewed manually.\n\n"
        "## Memory Updates Applied\n"
        "- Experiment memory was left unchanged.\n"
    )
