from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from Agents.Codex import run_codex_session

PROJECT_ROOT = Path(__file__).resolve().parent


def run_experiment_loop(
    target_repo: str | Path,
    eval_command: str,
    role: str = "experiment",
    num_iterations: int = 5,
    eval_strategy: str = "maximize",
):
    maximize = eval_strategy == "maximize"
    target_repo = Path(target_repo).resolve()
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

        worktree_path = worktree_dir / f"iteration_{i:03d}"

        try:
            _create_worktree(target_repo, worktree_path, base_commit)
        except subprocess.CalledProcessError as exc:
            print(f"Worktree creation failed: {exc}")
            result = _make_result(i, worktree_path, status="worktree_error", error=str(exc))
            results.append(result)
            _append_iteration(experiment_log, result)
            continue

        print(f"Worktree ready: {worktree_path}")

        instruction = (
            f"IMPORTANT: You must only create or modify files within your current "
            f"working directory ({worktree_path}). "
            f"Do not access, read, or modify any files outside this directory."
        )

        codex_response = ""
        codex_failed = False
        session_log = None
        start_time = time.time()
        try:
            session_result = run_codex_session(cwd=worktree_path, instruction=instruction, role=role)
            codex_response = session_result.turn_result.response_text
            session_log = session_result.session_log_path
            print(f"Codex done. Session log: {session_log}")
        except Exception as exc:
            codex_failed = True
            codex_response = str(exc)
            print(f"Codex failed: {exc}")
        codex_duration = round(time.time() - start_time, 1)

        eval_score, eval_error = _run_eval(eval_command, worktree_path)
        print(f"Eval score: {eval_score}")

        status = "codex_error" if codex_failed else ("eval_error" if eval_error else "completed")

        result = _make_result(
            i, worktree_path,
            session_log=str(session_log) if session_log else None,
            codex_response=codex_response,
            eval_score=eval_score,
            codex_duration_s=codex_duration,
            status=status,
            error=eval_error,
        )

        if status == "completed":
            parsed = _parse_score(eval_score)
            if parsed is not None:
                try:
                    commit_hash = _commit_and_branch(target_repo, worktree_path, i, parsed)
                    result["commit_hash"] = commit_hash
                    result["parsed_score"] = parsed
                    print(f"Saved to branch: experiment/iter_{i:03d}")
                except Exception as exc:
                    print(f"Warning: failed to save branch for iteration {i}: {exc}")

        results.append(result)
        _append_iteration(experiment_log, result)

    # Determine best iteration and save best branch
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


def _commit_and_branch(target_repo: Path, worktree_path: Path, iteration: int, score: float) -> str:
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", "-A"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "-m",
         f"Experiment iteration {iteration} - score: {score}"],
        capture_output=True, text=True, check=True,
    )
    commit_hash = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(target_repo), "branch",
         f"experiment/iter_{iteration:03d}", commit_hash],
        capture_output=True, text=True, check=True,
    )
    return commit_hash


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


def _run_eval(eval_command: str, worktree_path: Path) -> tuple[str, str]:
    """Returns (score_stdout, error_string). error_string is empty on success."""
    command = eval_command.replace("{worktree}", str(worktree_path))
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=300, cwd=str(worktree_path),
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
