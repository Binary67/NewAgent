from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..State.Workspace import snapshot_worktree

HIDDEN_EVAL_TOOL = {
    "name": "run_hidden_eval",
    "description": (
        "Request the hidden evaluation for your current changes. "
        "The orchestrator will run it after this turn and send the result in the next message. "
        "You have a limited number of calls -- use them wisely."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def parse_score(eval_score_str: str) -> float | None:
    lines = [line for line in eval_score_str.strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return float(lines[-1].strip())
    except ValueError:
        return None


def is_better(candidate: float, baseline: float, maximize: bool) -> bool:
    return candidate > baseline if maximize else candidate < baseline


def build_eval_handler(agent_worktree: Path, eval_state: dict[str, Any]):
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

        if eval_state["requested_this_turn"]:
            return {
                "success": True,
                "contentItems": [{"type": "inputText", "text": (
                    "=== EVALUATION ALREADY REQUESTED ===\n"
                    "You already requested an evaluation in this turn.\n"
                    "Do not make further edits or request another eval.\n"
                    "End this turn and wait for the next message with the result."
                )}],
            }

        trial_number = len(eval_state["trials"]) + 1
        try:
            commit_hash = snapshot_worktree(agent_worktree, trial_number)
        except Exception as exc:
            return {
                "success": False,
                "contentItems": [{"type": "inputText", "text": f"Evaluation request failed: {exc}"}],
            }

        eval_state["requested_this_turn"] = True
        eval_state["pending_request"] = {"commit": commit_hash}
        print(f"  [Eval requested] Trial {trial_number}: {commit_hash}")

        return {
            "success": True,
            "contentItems": [{"type": "inputText", "text": (
                "=== EVALUATION REQUESTED ===\n"
                f"Snapshot: {commit_hash}\n"
                "The orchestrator will run the hidden evaluation after this turn.\n"
                "Stop editing, do not request another eval in this turn, and wait for the next message with the result."
            )}],
        }

    return eval_handler


def apply_eval_overrides(eval_repo: Path, eval_worktree: Path, overrides: list[str]) -> None:
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


def run_requested_eval(
    eval_command: str,
    eval_worktree: Path,
    eval_repo_path: Path | None,
    eval_overrides: list[str],
    eval_state: dict[str, Any],
    pending_request: dict[str, Any],
    maximize: bool,
) -> str:
    commit_hash = pending_request["commit"]
    try:
        _sync_eval_worktree(eval_worktree, commit_hash)
        if eval_repo_path:
            apply_eval_overrides(eval_repo_path, eval_worktree, eval_overrides)

        score_stdout, score_error = run_eval(eval_command, eval_worktree)
        if score_error:
            return f"Evaluation error: {score_error}"

        parsed = parse_score(score_stdout)
        if parsed is None:
            return f"Could not parse score from eval output:\n{score_stdout}"

        eval_state["remaining"] -= 1
        eval_state["trials"].append({"commit": commit_hash, "score": parsed})

        trial_scores = [trial["score"] for trial in eval_state["trials"]]
        best_so_far = (max if maximize else min)(trial_scores)

        baseline_score = eval_state["baseline_score"]
        if baseline_score is not None:
            diff = parsed - baseline_score
            direction = "BETTER" if (diff > 0 if maximize else diff < 0) else "WORSE"
            diff_str = f"{diff:+.6f}"
            comparison_line = f"Comparison: {direction} ({diff_str} from baseline)"
        else:
            comparison_line = "Comparison: No baseline available"

        if baseline_score is not None and is_better(parsed, baseline_score, maximize):
            recommendation = "Your changes improved the score. You may stop here."
        else:
            recommendation = "Your changes did not improve the score. Analyze why and try a different approach."

        print(f"  [Eval trial {len(eval_state['trials'])}] Score: {parsed} ({comparison_line})")
        return (
            f"=== EVALUATION RESULT ===\n"
            f"Score: {parsed}\n"
            f"Baseline: {baseline_score if baseline_score is not None else 'N/A'}\n"
            f"Best so far: {best_so_far}\n"
            f"{comparison_line}\n"
            f"Remaining eval opportunities: {eval_state['remaining']}\n"
            f"RECOMMENDATION: {recommendation}"
        )
    except Exception as exc:
        return f"Evaluation error: {exc}"


def build_eval_followup_message(commit_hash: str, eval_feedback: str) -> str:
    return (
        f"Evaluation finished for snapshot {commit_hash}.\n\n"
        f"{eval_feedback}\n\n"
        "Continue from this result. Use another evaluation only after you complete a new hypothesis, "
        "then call `run_hidden_eval` once and end your turn."
    )


def run_eval(eval_command: str, eval_worktree: Path) -> tuple[str, str]:
    command = eval_command.replace("{worktree}", str(eval_worktree))
    command = command.replace("{eval_worktree}", str(eval_worktree))
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(eval_worktree),
        )
        if result.returncode == 0:
            return result.stdout.strip(), ""
        return "", f"exit {result.returncode}: {result.stderr.strip()}"
    except Exception as exc:
        return "", str(exc)


def _sync_eval_worktree(eval_worktree: Path, commit_hash: str) -> None:
    subprocess.run(
        ["git", "-C", str(eval_worktree), "reset", "--hard", commit_hash],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(eval_worktree), "clean", "-fdx"],
        capture_output=True,
        text=True,
        check=True,
    )
