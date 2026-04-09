from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

from Agents.Codex import run_codex_session

PROJECT_ROOT = Path(__file__).resolve().parent


def run_experiment_loop(
    target_repo: str | Path,
    eval_command: str,
    codex_instruction: str,
    num_iterations: int = 5,
):
    target_repo = Path(target_repo).resolve()
    worktree_dir = PROJECT_ROOT / "Worktrees"
    logs_dir = PROJECT_ROOT / "Logs"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    base_commit = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    experiment_log = logs_dir / f"experiment_{datetime.now():%Y%m%d_%H%M%S}.md"
    _write_header(experiment_log, target_repo, base_commit, eval_command, num_iterations)

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
            f"Do not access, read, or modify any files outside this directory.\n\n"
            f"{codex_instruction}"
        )

        codex_response = ""
        codex_failed = False
        session_log = None
        start_time = time.time()
        try:
            session_result = run_codex_session(cwd=worktree_path, instruction=instruction, role="experiment")
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
        results.append(result)
        _append_iteration(experiment_log, result)

    _append_summary(experiment_log, results)
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


def _write_header(log_path: Path, target_repo: Path, base_commit: str, eval_command: str, num_iterations: int):
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("# Experiment Log\n\n")
        f.write(f"- **Started**: {datetime.now().isoformat()}\n")
        f.write(f"- **Target Repo**: `{target_repo}`\n")
        f.write(f"- **Base Commit**: `{base_commit}`\n")
        f.write(f"- **Iterations**: {num_iterations}\n")
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


def _append_summary(log_path: Path, results: list[dict]):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("## Summary\n\n")
        f.write("| Iteration | Status | Eval Score | Duration |\n")
        f.write("|-----------|--------|------------|----------|\n")
        for r in results:
            f.write(f"| {r['iteration']} | {r['status']} | {r['eval_score']} | {r['codex_duration_s']}s |\n")
        f.write(f"\n- **Completed**: {datetime.now().isoformat()}\n")
