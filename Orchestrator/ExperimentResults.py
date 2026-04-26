from __future__ import annotations

from pathlib import Path


def make_result(iteration: int, worktree_path: Path, **kwargs):
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


def build_iteration_record(run_id: str, result: dict[str, object]) -> dict[str, object]:
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
