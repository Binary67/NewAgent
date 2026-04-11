from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from Agents.Codex import run_codex_session

PROJECT_ROOT = Path(__file__).resolve().parent
BEST_BRANCH = "best/current"
BEST_STATE_PATH = PROJECT_ROOT / "BestState.json"

HIDDEN_EVAL_TOOL = {
    "name": "run_hidden_eval",
    "description": (
        "Run the hidden evaluation on your current changes. "
        "Returns a score and comparison against the baseline. "
        "You have a limited number of calls -- use them wisely."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


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
        current_best_commit, current_best_score = _load_best_state(target_repo, eval_strategy)
    except RuntimeError as exc:
        print(exc)
        return []

    if current_best_commit:
        print(f"Resuming from {BEST_BRANCH}: {current_best_commit} (score: {current_best_score})")
    else:
        current_best_commit = _get_head_commit(target_repo)
        print(f"Starting from HEAD: {current_best_commit}")

    initial_commit = current_best_commit
    experiment_log = logs_dir / f"experiment_{datetime.now():%Y%m%d_%H%M%S}.md"
    _write_header(experiment_log, target_repo, initial_commit, eval_command, num_iterations, eval_strategy)

    results = []
    fatal_error = ""

    for i in range(1, num_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"  Iteration {i} / {num_iterations}")
        print(f"{'=' * 60}")

        base_commit = current_best_commit
        agent_worktree = worktree_dir / f"iteration_{i:03d}_agent"
        eval_worktree = worktree_dir / f"iteration_{i:03d}_eval"

        try:
            _create_worktree(target_repo, agent_worktree, base_commit)
            _create_worktree(target_repo, eval_worktree, base_commit)
        except subprocess.CalledProcessError as exc:
            print(f"Worktree creation failed: {exc}")
            result = _make_result(
                i,
                agent_worktree,
                base_commit=base_commit,
                status="worktree_error",
                error=str(exc),
            )
            results.append(result)
            _append_iteration(experiment_log, result)
            continue

        if eval_repo_path:
            _apply_eval_overrides(eval_repo_path, eval_worktree, eval_overrides)

        print(f"Agent worktree ready: {agent_worktree}")
        print(f"Eval worktree ready:  {eval_worktree}")

        if prewarm_command:
            agent_prewarm_ok, agent_prewarm_error = _run_prewarm_command(
                agent_worktree,
                prewarm_command,
                action="Prewarming agent worktree",
            )
            if not agent_prewarm_ok:
                print(agent_prewarm_error)
                result = _make_result(
                    i,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=agent_prewarm_error,
                )
                results.append(result)
                _append_iteration(experiment_log, result)
                continue

            eval_prewarm_ok, eval_prewarm_error = _run_prewarm_command(
                eval_worktree,
                prewarm_command,
                action="Prewarming eval worktree",
            )
            if not eval_prewarm_ok:
                print(eval_prewarm_error)
                result = _make_result(
                    i,
                    agent_worktree,
                    base_commit=base_commit,
                    status="setup_error",
                    error=eval_prewarm_error,
                )
                results.append(result)
                _append_iteration(experiment_log, result)
                continue

        eval_prewarm_state = _get_prewarm_watch_state(eval_worktree, prewarm_watch_files)

        baseline_stdout, baseline_error = _run_eval(eval_command, eval_worktree)
        baseline_score = _parse_score(baseline_stdout) if not baseline_error else None
        if baseline_score is not None:
            print(f"Baseline score: {baseline_score}")
        else:
            print(f"Baseline eval failed: {baseline_error or 'unparseable output'}")

        eval_state: dict[str, Any] = {
            "remaining": max_eval_calls,
            "baseline_score": baseline_score,
            "trials": [],
            "prewarm_state": eval_prewarm_state,
        }

        def eval_handler(tool_name: str, arguments: Any) -> dict[str, Any]:
            if tool_name != "run_hidden_eval":
                return {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": f"Unknown tool: {tool_name}"}],
                }

            if eval_state["remaining"] <= 0:
                return {
                    "success": True,
                    "contentItems": [{"type": "inputText", "text": (
                        "=== EVALUATION UNAVAILABLE ===\n"
                        "You have used all your eval opportunities.\n"
                        "Continue refining based on your best judgment."
                    )}],
                }

            commit_hash = _snapshot_worktree(agent_worktree, len(eval_state["trials"]) + 1)
            _sync_eval_worktree(eval_worktree, commit_hash)
            if eval_repo_path:
                _apply_eval_overrides(eval_repo_path, eval_worktree, eval_overrides)
            prewarm_ok, prewarm_error, prewarm_state = _sync_eval_worktree_prewarm_if_needed(
                eval_worktree,
                prewarm_command,
                prewarm_watch_files,
                eval_state["prewarm_state"],
            )
            if not prewarm_ok:
                return {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": f"Evaluation error: {prewarm_error}"}],
                }

            eval_state["prewarm_state"] = prewarm_state
            score_stdout, score_error = _run_eval(eval_command, eval_worktree)

            if score_error:
                return {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": f"Evaluation error: {score_error}"}],
                }

            parsed = _parse_score(score_stdout)
            if parsed is None:
                return {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": f"Could not parse score from eval output:\n{score_stdout}"}],
                }

            eval_state["remaining"] -= 1
            eval_state["trials"].append({"commit": commit_hash, "score": parsed})

            trial_scores = [t["score"] for t in eval_state["trials"]]
            best_so_far = (max if maximize else min)(trial_scores)

            bl = eval_state["baseline_score"]
            if bl is not None:
                diff = parsed - bl
                direction = "BETTER" if (diff > 0 if maximize else diff < 0) else "WORSE"
                diff_str = f"{diff:+.6f}"
                comparison_line = f"Comparison: {direction} ({diff_str} from baseline)"
            else:
                comparison_line = "Comparison: No baseline available"

            if bl is not None and _is_better(parsed, bl, maximize):
                recommendation = "Your changes improved the score. You may stop here."
            else:
                recommendation = "Your changes did not improve the score. Analyze why and try a different approach."

            feedback = (
                f"=== EVALUATION RESULT ===\n"
                f"Score: {parsed}\n"
                f"Baseline: {bl if bl is not None else 'N/A'}\n"
                f"Best so far: {best_so_far}\n"
                f"{comparison_line}\n"
                f"Remaining eval opportunities: {eval_state['remaining']}\n"
                f"RECOMMENDATION: {recommendation}"
            )
            print(f"  [Eval trial {len(eval_state['trials'])}] Score: {parsed} ({comparison_line})")

            return {
                "success": True,
                "contentItems": [{"type": "inputText", "text": feedback}],
            }

        instruction = (
            f"IMPORTANT: You must only create or modify files within your current "
            f"working directory ({agent_worktree}). "
            f"Do not access, read, or modify any files outside this directory."
        )

        codex_response = ""
        codex_failed = False
        session_log = None
        start_time = time.time()
        try:
            session_result = run_codex_session(
                cwd=agent_worktree,
                instruction=instruction,
                role=role,
                dynamic_tools=[HIDDEN_EVAL_TOOL],
                tool_handler=eval_handler,
            )
            codex_response = session_result.turn_result.response_text
            session_log = session_result.session_log_path
            print(f"Codex done. Session log: {session_log}")
        except Exception as exc:
            codex_failed = True
            codex_response = str(exc)
            print(f"Codex failed: {exc}")
        codex_duration = round(time.time() - start_time, 1)

        trials = eval_state["trials"]
        best_trial = None
        if trials:
            best_trial = (max if maximize else min)(trials, key=lambda t: t["score"])

        promotion_error = ""
        best_score_str = str(best_trial["score"]) if best_trial else ""
        status = "codex_error" if codex_failed else "completed"

        result = _make_result(
            i,
            agent_worktree,
            base_commit=base_commit,
            session_log=str(session_log) if session_log else None,
            codex_response=codex_response,
            eval_score=best_score_str,
            codex_duration_s=codex_duration,
            status=status,
            error="" if not codex_failed else codex_response,
            trials=trials,
            promoted_to_best=False,
        )

        if best_trial:
            result["commit_hash"] = best_trial["commit"]
            result["parsed_score"] = best_trial["score"]
            try:
                subprocess.run(
                    ["git", "-C", str(target_repo), "branch", "-f", f"experiment/iter_{i:03d}", best_trial["commit"]],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                print(f"Saved best trial to branch: experiment/iter_{i:03d} (score: {best_trial['score']})")
            except Exception as exc:
                print(f"Warning: failed to save branch for iteration {i}: {exc}")

            should_promote = current_best_score is None or _is_better(best_trial["score"], current_best_score, maximize)
            if should_promote:
                try:
                    _promote_best_state(target_repo, best_trial["commit"], best_trial["score"], eval_strategy)
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
        _append_iteration(experiment_log, result)

        if promotion_error:
            fatal_error = promotion_error
            break

    _delete_branches(target_repo, "experiment/iter_*")

    completed_results = [r for r in results if r.get("parsed_score") is not None]
    best_result = None

    if completed_results:
        best_result = (max if maximize else min)(completed_results, key=lambda r: r["parsed_score"])
    elif not fatal_error:
        print("No successful iterations. Keeping existing best state (if any).")

    _append_summary(experiment_log, results, best_result, fatal_error=fatal_error)

    if fatal_error:
        print(f"\nExperiment stopped early. Log: {experiment_log}")
    else:
        print(f"\nExperiment complete. Log: {experiment_log}")
    return results


def _snapshot_worktree(worktree_path: Path, trial_number: int) -> str:
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", "-A"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "--allow-empty", "-m", f"Trial {trial_number} snapshot"],
        capture_output=True,
        text=True,
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_result(iteration, worktree_path, **kwargs):
    return {
        "iteration": iteration,
        "worktree": str(worktree_path),
        "base_commit": "",
        "session_log": None,
        "codex_response": "",
        "eval_score": "",
        "codex_duration_s": 0,
        "status": "completed",
        "error": "",
        "promoted_to_best": False,
        **kwargs,
    }


def _parse_score(eval_score_str: str) -> float | None:
    lines = [l for l in eval_score_str.strip().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1].strip())
    except ValueError:
        return None


def _is_better(candidate: float, baseline: float, maximize: bool) -> bool:
    return candidate > baseline if maximize else candidate < baseline


def _load_best_state(target_repo: Path, eval_strategy: str) -> tuple[str, float | None]:
    best_branches = _list_branches(target_repo, "best/*")
    has_best_branch = BEST_BRANCH in best_branches
    stray_best_branches = [branch for branch in best_branches if branch != BEST_BRANCH]
    has_best_state_file = BEST_STATE_PATH.exists()

    if not has_best_branch and not has_best_state_file:
        if stray_best_branches:
            branch_list = ", ".join(stray_best_branches)
            raise RuntimeError(
                f"Legacy best branch state detected ({branch_list}). "
                "Run ResetExperiments.py first."
            )
        return ("", None)

    if has_best_branch != has_best_state_file:
        raise RuntimeError(
            f"Invalid best state for {target_repo}: {BEST_BRANCH} and {BEST_STATE_PATH.name} "
            "must both exist or both be absent. Run ResetExperiments.py first."
        )

    best_commit = _resolve_branch_commit(target_repo, BEST_BRANCH)
    if not best_commit:
        raise RuntimeError(
            f"Invalid best state for {target_repo}: could not resolve {BEST_BRANCH}. "
            "Run ResetExperiments.py first."
        )

    try:
        raw_state = json.loads(BEST_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: {exc}. "
            "Run ResetExperiments.py first."
        ) from exc

    if not isinstance(raw_state, dict):
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: expected an object. "
            "Run ResetExperiments.py first."
        )

    state_repo = raw_state.get("target_repo")
    state_branch = raw_state.get("best_branch")
    state_commit = raw_state.get("best_commit")
    state_score = raw_state.get("best_score")
    state_strategy = raw_state.get("eval_strategy")
    updated_at = raw_state.get("updated_at")

    if state_repo != str(target_repo):
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: target_repo does not match {target_repo}. "
            "Run ResetExperiments.py first."
        )
    if state_branch != BEST_BRANCH:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: best_branch must be {BEST_BRANCH}. "
            "Run ResetExperiments.py first."
        )
    if state_strategy != eval_strategy:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: eval_strategy does not match {eval_strategy}. "
            "Run ResetExperiments.py first."
        )
    if not isinstance(state_commit, str) or not state_commit:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: best_commit is missing. "
            "Run ResetExperiments.py first."
        )
    if state_commit != best_commit:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: best_commit does not match {BEST_BRANCH}. "
            "Run ResetExperiments.py first."
        )
    if not isinstance(state_score, (int, float)) or isinstance(state_score, bool):
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: best_score must be numeric. "
            "Run ResetExperiments.py first."
        )
    if not isinstance(updated_at, str) or not updated_at:
        raise RuntimeError(
            f"Invalid best state metadata at {BEST_STATE_PATH}: updated_at is missing. "
            "Run ResetExperiments.py first."
        )

    _cleanup_stray_best_branches(target_repo, stray_best_branches)
    return (best_commit, float(state_score))


def _write_best_state(target_repo: Path, best_commit: str, best_score: float, eval_strategy: str) -> None:
    best_state = {
        "target_repo": str(target_repo),
        "best_branch": BEST_BRANCH,
        "best_commit": best_commit,
        "best_score": best_score,
        "eval_strategy": eval_strategy,
        "updated_at": datetime.now().isoformat(),
    }
    temp_path = BEST_STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(f"{json.dumps(best_state, indent=2)}\n", encoding="utf-8")
    temp_path.replace(BEST_STATE_PATH)


def _promote_best_state(target_repo: Path, best_commit: str, best_score: float, eval_strategy: str) -> None:
    subprocess.run(
        ["git", "-C", str(target_repo), "branch", "-f", BEST_BRANCH, best_commit],
        capture_output=True,
        text=True,
        check=True,
    )
    _write_best_state(target_repo, best_commit, best_score, eval_strategy)
    _cleanup_stray_best_branches(target_repo)


def _get_head_commit(target_repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _resolve_branch_commit(target_repo: Path, branch_name: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "--verify", f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _list_branches(target_repo: Path, pattern: str) -> list[str]:
    output = subprocess.run(
        ["git", "-C", str(target_repo), "branch", "--format=%(refname:short)", "--list", pattern],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return [line.strip() for line in output.splitlines() if line.strip()]


def _cleanup_stray_best_branches(target_repo: Path, stray_branches: list[str] | None = None) -> None:
    branches = stray_branches if stray_branches is not None else [
        branch for branch in _list_branches(target_repo, "best/*") if branch != BEST_BRANCH
    ]
    for branch_name in branches:
        result = subprocess.run(
            ["git", "-C", str(target_repo), "branch", "-D", branch_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Deleted stray best branch: {branch_name}")
        else:
            stderr = result.stderr.strip() or "unknown error"
            print(f"Warning: failed to delete stray best branch {branch_name}: {stderr}")


def _delete_branches(target_repo: Path, pattern: str) -> None:
    for name in _list_branches(target_repo, pattern):
        result = subprocess.run(
            ["git", "-C", str(target_repo), "branch", "-D", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Deleted branch: {name}")
        else:
            stderr = result.stderr.strip() or "unknown error"
            print(f"Warning: failed to delete branch {name}: {stderr}")


def _create_worktree(target_repo: Path, worktree_path: Path, commit: str):
    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", str(target_repo), "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(target_repo), "worktree", "prune"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(target_repo), "worktree", "add", "--detach", str(worktree_path), commit],
        capture_output=True,
        text=True,
        check=True,
    )


def _build_prewarm_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "__PYVENV_LAUNCHER__"):
        environment.pop(key, None)
    return environment


def _run_prewarm_command(worktree_path: Path, prewarm_command: str, *, action: str) -> tuple[bool, str]:
    print(f"{action}: {worktree_path}")
    try:
        result = subprocess.run(
            prewarm_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(worktree_path),
            env=_build_prewarm_environment(),
        )
    except subprocess.TimeoutExpired:
        return (False, f"{action} failed for {worktree_path}: TIMEOUT")
    except Exception as exc:
        return (False, f"{action} failed for {worktree_path}: {exc}")

    if result.returncode == 0:
        print(f"{action} complete: {worktree_path}")
        return (True, "")

    details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    suffix = f":\n{details}" if details else ""
    return (False, f"{action} failed for {worktree_path} (exit {result.returncode}){suffix}")


def _get_prewarm_watch_state(worktree_path: Path, prewarm_watch_files: list[str]) -> tuple[tuple[str, bool, int, int], ...]:
    state: list[tuple[str, bool, int, int]] = []
    for relative_path in prewarm_watch_files:
        path = worktree_path / relative_path
        if not path.exists():
            state.append((relative_path, False, -1, -1))
            continue
        stat = path.stat()
        state.append((relative_path, True, stat.st_size, stat.st_mtime_ns))
    return tuple(state)


def _sync_eval_worktree_prewarm_if_needed(
    eval_worktree: Path,
    prewarm_command: str,
    prewarm_watch_files: list[str],
    previous_state: tuple[tuple[str, bool, int, int], ...],
) -> tuple[bool, str, tuple[tuple[str, bool, int, int], ...]]:
    if not prewarm_command:
        return (True, "", previous_state)

    if not prewarm_watch_files:
        print(f"No prewarm watch files configured; skipping eval prewarm check: {eval_worktree}")
        return (True, "", previous_state)

    current_state = _get_prewarm_watch_state(eval_worktree, prewarm_watch_files)
    if current_state == previous_state:
        print(f"Eval prewarm watch files unchanged; skipping prewarm: {eval_worktree}")
        return (True, "", previous_state)

    print(f"Eval prewarm watch files changed; rerunning prewarm: {eval_worktree}")
    prewarm_ok, prewarm_error = _run_prewarm_command(
        eval_worktree,
        prewarm_command,
        action="Rewarming eval worktree",
    )
    if not prewarm_ok:
        return (False, prewarm_error, previous_state)

    return (True, "", _get_prewarm_watch_state(eval_worktree, prewarm_watch_files))


def _sync_eval_worktree(eval_worktree: Path, commit_hash: str) -> None:
    subprocess.run(
        ["git", "-C", str(eval_worktree), "checkout", commit_hash, "--", "."],
        capture_output=True,
        text=True,
        check=True,
    )


def _apply_eval_overrides(eval_repo: Path, eval_worktree: Path, overrides: list[str]) -> None:
    for pattern in overrides:
        matches = list(eval_repo.glob(pattern))
        if not matches:
            print(f"  Warning: eval_overrides pattern '{pattern}' matched no files in {eval_repo}")
        for src in matches:
            if not src.is_file():
                continue
            rel = src.relative_to(eval_repo)
            dst = eval_worktree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _run_eval(eval_command: str, eval_worktree: Path) -> tuple[str, str]:
    """Returns (score_stdout, error_string). error_string is empty on success."""
    command = eval_command.replace("{worktree}", str(eval_worktree))
    command = command.replace("{eval_worktree}", str(eval_worktree))
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(eval_worktree),
        )
        if result.returncode == 0:
            return result.stdout.strip(), ""
        return "", f"exit {result.returncode}: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as exc:
        return "", str(exc)


def _write_header(log_path: Path, target_repo: Path, initial_commit: str, eval_command: str, num_iterations: int, eval_strategy: str):
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("# Experiment Log\n\n")
        f.write(f"- **Started**: {datetime.now().isoformat()}\n")
        f.write(f"- **Target Repo**: `{target_repo}`\n")
        f.write(f"- **Initial Commit**: `{initial_commit}`\n")
        f.write(f"- **Iterations**: {num_iterations}\n")
        f.write(f"- **Eval Strategy**: {eval_strategy}\n")
        f.write(f"- **Eval Command**: `{eval_command}`\n\n")


def _append_iteration(log_path: Path, result: dict):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"## Iteration {result['iteration']}\n\n")
        f.write(f"- **Status**: {result['status']}\n")
        f.write(f"- **Base Commit**: `{result['base_commit']}`\n")
        f.write(f"- **Eval Score**: {result['eval_score']}\n")
        f.write(f"- **Promoted To {BEST_BRANCH}**: {'yes' if result['promoted_to_best'] else 'no'}\n")
        if result["codex_duration_s"]:
            f.write(f"- **Codex Duration**: {result['codex_duration_s']}s\n")
        f.write(f"- **Worktree**: `{result['worktree']}`\n")
        if result["session_log"]:
            f.write(f"- **Session Log**: `{result['session_log']}`\n")
        if result["error"]:
            f.write(f"- **Error**: {result['error']}\n")
        if result["codex_response"]:
            f.write(f"\n### Codex Response\n\n{result['codex_response']}\n")
        f.write("\n---\n\n")


def _append_summary(log_path: Path, results: list[dict], best_result: dict | None = None, fatal_error: str = ""):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("## Summary\n\n")
        f.write("| Iteration | Status | Eval Score | Duration |\n")
        f.write("|-----------|--------|------------|----------|\n")
        for r in results:
            f.write(f"| {r['iteration']} | {r['status']} | {r['eval_score']} | {r['codex_duration_s']}s |\n")
        if best_result:
            f.write(f"\n- **Best Iteration**: {best_result['iteration']} (score: {best_result.get('parsed_score', 'N/A')})\n")
        if fatal_error:
            f.write(f"- **Stopped Early**: {fatal_error}\n")
        f.write(f"\n- **Completed**: {datetime.now().isoformat()}\n")
