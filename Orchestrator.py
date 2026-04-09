from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from Agents.Codex import run_codex_session

PROJECT_ROOT = Path(__file__).resolve().parent

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
):
    maximize = eval_strategy == "maximize"
    target_repo = Path(target_repo).resolve()
    eval_repo_path = Path(eval_repo).resolve() if eval_repo else None
    eval_overrides = eval_overrides or []
    worktree_dir = PROJECT_ROOT / "Worktrees"
    logs_dir = PROJECT_ROOT / "Logs"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    prev_best_commit, prev_best_score = _get_best_branch_info(target_repo)
    if prev_best_commit:
        base_commit = prev_best_commit
        print(f"Resuming from previous best: {base_commit} (score: {prev_best_score})")
    else:
        base_commit = subprocess.run(
            ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        print(f"Starting from HEAD: {base_commit}")

    experiment_log = logs_dir / f"experiment_{datetime.now():%Y%m%d_%H%M%S}.md"
    _write_header(experiment_log, target_repo, base_commit, eval_command, num_iterations, eval_strategy)

    results = []
    for i in range(1, num_iterations + 1):
        print(f"\n{'=' * 60}")
        print(f"  Iteration {i} / {num_iterations}")
        print(f"{'=' * 60}")

        agent_worktree = worktree_dir / f"iteration_{i:03d}_agent"
        eval_worktree = worktree_dir / f"iteration_{i:03d}_eval"

        try:
            _create_worktree(target_repo, agent_worktree, base_commit)
            _create_worktree(target_repo, eval_worktree, base_commit)
        except subprocess.CalledProcessError as exc:
            print(f"Worktree creation failed: {exc}")
            result = _make_result(i, agent_worktree, status="worktree_error", error=str(exc))
            results.append(result)
            _append_iteration(experiment_log, result)
            continue

        if eval_repo_path:
            _apply_eval_overrides(eval_repo_path, eval_worktree, eval_overrides)

        print(f"Agent worktree ready: {agent_worktree}")
        print(f"Eval worktree ready:  {eval_worktree}")

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

            if bl is not None and (parsed > bl if maximize else parsed < bl):
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

        best_score_str = str(best_trial["score"]) if best_trial else ""
        status = "codex_error" if codex_failed else "completed"

        result = _make_result(
            i, agent_worktree,
            session_log=str(session_log) if session_log else None,
            codex_response=codex_response,
            eval_score=best_score_str,
            codex_duration_s=codex_duration,
            status=status,
            error="" if not codex_failed else codex_response,
            trials=trials,
        )

        if best_trial:
            result["commit_hash"] = best_trial["commit"]
            result["parsed_score"] = best_trial["score"]
            try:
                subprocess.run(
                    ["git", "-C", str(target_repo), "branch",
                     f"experiment/iter_{i:03d}", best_trial["commit"]],
                    capture_output=True, text=True, check=True,
                )
                print(f"Saved best trial to branch: experiment/iter_{i:03d} (score: {best_trial['score']})")
            except Exception as exc:
                print(f"Warning: failed to save branch for iteration {i}: {exc}")

        results.append(result)
        _append_iteration(experiment_log, result)

    completed_results = [r for r in results if r.get("parsed_score") is not None]
    best_result = None

    if completed_results:
        best_result = (max if maximize else min)(completed_results, key=lambda r: r["parsed_score"])
        new_best_score = best_result["parsed_score"]
        new_best_iter = best_result["iteration"]
        new_best_commit = best_result.get("commit_hash", "")

        should_update = prev_best_score is None or (
            new_best_score > prev_best_score if maximize else new_best_score < prev_best_score
        )

        if should_update and new_best_commit:
            _delete_branches(target_repo, "best/*")
            subprocess.run(
                ["git", "-C", str(target_repo), "branch",
                 f"best/iter_{new_best_iter:03d}", new_best_commit],
                capture_output=True, text=True, check=True,
            )
            print(f"Best branch: best/iter_{new_best_iter:03d} (score: {new_best_score})")
        elif not should_update:
            print(f"No improvement over previous best ({prev_best_score}). Keeping existing best branch.")

        _delete_branches(target_repo, "experiment/iter_*")
    else:
        print("No successful iterations. Keeping existing best branch (if any).")

    _append_summary(experiment_log, results, best_result)
    print(f"\nExperiment complete. Log: {experiment_log}")
    return results


def _snapshot_worktree(worktree_path: Path, trial_number: int) -> str:
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", "-A"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "--allow-empty", "-m",
         f"Trial {trial_number} snapshot"],
        capture_output=True, text=True, check=True,
    )
    return subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_result(iteration, worktree_path, **kwargs):
    return {
        "iteration": iteration,
        "worktree": str(worktree_path),
        "session_log": None,
        "codex_response": "",
        "eval_score": "",
        "codex_duration_s": 0,
        "status": "completed",
        "error": "",
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



def _get_best_branch_info(target_repo: Path) -> tuple[str, float | None]:
    output = subprocess.run(
        ["git", "-C", str(target_repo), "branch", "--list", "best/*"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not output:
        return ("", None)
    branch_name = output.splitlines()[0].strip().removeprefix("* ")
    commit_hash = subprocess.run(
        ["git", "-C", str(target_repo), "log", "-1", "--format=%H", branch_name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    message = subprocess.run(
        ["git", "-C", str(target_repo), "log", "-1", "--format=%s", branch_name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    match = re.search(r"score:\s*([-\d.eE+]+)", message)
    score = float(match.group(1)) if match else None
    return (commit_hash, score)


def _delete_branches(target_repo: Path, pattern: str):
    output = subprocess.run(
        ["git", "-C", str(target_repo), "branch", "--list", pattern],
        capture_output=True, text=True,
    ).stdout.strip()
    for line in output.splitlines():
        name = line.strip().removeprefix("* ")
        if name:
            subprocess.run(
                ["git", "-C", str(target_repo), "branch", "-D", name],
                capture_output=True,
            )


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
        capture_output=True, text=True, check=True,
    )


def _sync_eval_worktree(eval_worktree: Path, commit_hash: str) -> None:
    subprocess.run(
        ["git", "-C", str(eval_worktree), "checkout", commit_hash, "--", "."],
        capture_output=True, text=True, check=True,
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
            command, shell=True, capture_output=True, text=True,
            timeout=300, cwd=str(eval_worktree),
        )
        if result.returncode == 0:
            return result.stdout.strip(), ""
        return "", f"exit {result.returncode}: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT"
    except Exception as exc:
        return "", str(exc)


def _write_header(log_path: Path, target_repo: Path, base_commit: str, eval_command: str, num_iterations: int, eval_strategy: str):
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("# Experiment Log\n\n")
        f.write(f"- **Started**: {datetime.now().isoformat()}\n")
        f.write(f"- **Target Repo**: `{target_repo}`\n")
        f.write(f"- **Base Commit**: `{base_commit}`\n")
        f.write(f"- **Iterations**: {num_iterations}\n")
        f.write(f"- **Eval Strategy**: {eval_strategy}\n")
        f.write(f"- **Eval Command**: `{eval_command}`\n\n")


def _append_iteration(log_path: Path, result: dict):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"## Iteration {result['iteration']}\n\n")
        f.write(f"- **Status**: {result['status']}\n")
        f.write(f"- **Eval Score**: {result['eval_score']}\n")
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


def _append_summary(log_path: Path, results: list[dict], best_result: dict | None = None):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("## Summary\n\n")
        f.write("| Iteration | Status | Eval Score | Duration |\n")
        f.write("|-----------|--------|------------|----------|\n")
        for r in results:
            f.write(f"| {r['iteration']} | {r['status']} | {r['eval_score']} | {r['codex_duration_s']}s |\n")
        if best_result:
            f.write(f"\n- **Best Iteration**: {best_result['iteration']} (score: {best_result.get('parsed_score', 'N/A')})\n")
        f.write(f"\n- **Completed**: {datetime.now().isoformat()}\n")
