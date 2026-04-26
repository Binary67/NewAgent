from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from .BestState import BEST_BRANCH, load_best_state, promote_best_state
from .Evaluation import (
    apply_eval_overrides,
    get_prewarm_watch_state,
    is_better,
    parse_score,
    run_eval,
    run_prewarm_command,
)
from .ExperimentLog import append_iteration, append_summary, write_header
from .ExperimentResults import build_iteration_record, make_result
from .ExperimentSession import run_iteration_session
from .Learning import (
    append_iteration_record,
    ensure_default_experiment_memory,
    list_changed_files,
)
from .Reflection import run_reflection
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
            result = make_result(
                iteration,
                agent_worktree,
                base_commit=base_commit,
                status="worktree_error",
                error=str(exc),
            )
            results.append(result)
            append_iteration(experiment_log, result, BEST_BRANCH)
            append_iteration_record(iteration_record_path, build_iteration_record(run_id, result))
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
                result = make_result(
                    iteration,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=agent_prewarm_error,
                )
                results.append(result)
                append_iteration(experiment_log, result, BEST_BRANCH)
                append_iteration_record(iteration_record_path, build_iteration_record(run_id, result))
                continue

            eval_prewarm_ok, eval_prewarm_error = run_prewarm_command(
                eval_worktree,
                prewarm_command,
                action="Prewarming eval worktree",
            )
            if not eval_prewarm_ok:
                print(eval_prewarm_error)
                result = make_result(
                    iteration,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=eval_prewarm_error,
                )
                results.append(result)
                append_iteration(experiment_log, result, BEST_BRANCH)
                append_iteration_record(iteration_record_path, build_iteration_record(run_id, result))
                continue

        eval_prewarm_state = get_prewarm_watch_state(eval_worktree, prewarm_watch_files)

        baseline_stdout, baseline_error = run_eval(eval_command, eval_worktree)
        baseline_score = parse_score(baseline_stdout) if not baseline_error else None
        if baseline_score is not None:
            print(f"Baseline score: {baseline_score}")
        else:
            print(f"Baseline eval failed: {baseline_error or 'unparseable output'}")

        session_result = run_iteration_session(
            iteration=iteration,
            agent_worktree=agent_worktree,
            eval_worktree=eval_worktree,
            role=role,
            eval_command=eval_command,
            eval_repo_path=eval_repo_path,
            eval_overrides=eval_overrides,
            prewarm_command=prewarm_command,
            prewarm_watch_files=prewarm_watch_files,
            baseline_score=baseline_score,
            max_eval_calls=max_eval_calls,
            eval_prewarm_state=eval_prewarm_state,
            maximize=maximize,
        )

        trials = session_result["trials"]
        best_trial = None
        if trials:
            best_trial = (max if maximize else min)(trials, key=lambda trial: trial["score"])

        promotion_error = ""
        best_score_str = str(best_trial["score"]) if best_trial else ""

        result = make_result(
            iteration,
            agent_worktree,
            base_commit=base_commit,
            eval_score=best_score_str,
            baseline_score=baseline_score,
            promoted_to_best=False,
            **session_result,
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
        append_iteration_record(iteration_record_path, build_iteration_record(run_id, result))

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
    run_reflection(
        results,
        maximize,
        run_id,
        iteration_record_path,
        run_reflection_path,
        experiment_memory_path,
        PROJECT_ROOT,
    )

    if fatal_error:
        print(f"\nExperiment stopped early. Log: {experiment_log}")
    else:
        print(f"\nExperiment complete. Log: {experiment_log}")
    return results
