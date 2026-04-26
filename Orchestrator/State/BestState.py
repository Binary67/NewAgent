from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from .Workspace import cleanup_stray_best_branches, list_branches, resolve_branch_commit

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BEST_BRANCH = "best/current"
BEST_STATE_PATH = PROJECT_ROOT / "BestState.json"


def load_best_state(target_repo: Path, eval_strategy: str) -> tuple[str, float | None]:
    best_branches = list_branches(target_repo, "best/*")
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

    best_commit = resolve_branch_commit(target_repo, BEST_BRANCH)
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

    cleanup_stray_best_branches(target_repo, BEST_BRANCH, stray_best_branches)
    return (best_commit, float(state_score))


def write_best_state(target_repo: Path, best_commit: str, best_score: float, eval_strategy: str) -> None:
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


def promote_best_state(target_repo: Path, best_commit: str, best_score: float, eval_strategy: str) -> None:
    subprocess.run(
        ["git", "-C", str(target_repo), "branch", "-f", BEST_BRANCH, best_commit],
        capture_output=True,
        text=True,
        check=True,
    )
    write_best_state(target_repo, best_commit, best_score, eval_strategy)
    cleanup_stray_best_branches(target_repo, BEST_BRANCH)
