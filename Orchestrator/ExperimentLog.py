from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def write_header(
    log_path: Path,
    target_repo: Path,
    initial_commit: str,
    eval_command: str,
    num_iterations: int,
    eval_strategy: str,
) -> None:
    with log_path.open("w", encoding="utf-8") as file:
        file.write("# Experiment Log\n\n")
        file.write(f"- **Started**: {datetime.now().isoformat()}\n")
        file.write(f"- **Target Repo**: `{target_repo}`\n")
        file.write(f"- **Initial Commit**: `{initial_commit}`\n")
        file.write(f"- **Iterations**: {num_iterations}\n")
        file.write(f"- **Eval Strategy**: {eval_strategy}\n")
        file.write(f"- **Eval Command**: `{eval_command}`\n\n")


def append_iteration(log_path: Path, result: dict[str, object], best_branch: str) -> None:
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"## Iteration {result['iteration']}\n\n")
        file.write(f"- **Status**: {result['status']}\n")
        file.write(f"- **Base Commit**: `{result['base_commit']}`\n")
        file.write(f"- **Eval Score**: {result['eval_score']}\n")
        file.write(f"- **Promoted To {best_branch}**: {'yes' if result['promoted_to_best'] else 'no'}\n")
        if result["codex_duration_s"]:
            file.write(f"- **Codex Duration**: {result['codex_duration_s']}s\n")
        file.write(f"- **Worktree**: `{result['worktree']}`\n")
        if result["session_log"]:
            file.write(f"- **Session Log**: `{result['session_log']}`\n")
        if result["error"]:
            file.write(f"- **Error**: {result['error']}\n")
        if result["codex_response"]:
            file.write(f"\n### Codex Response\n\n{result['codex_response']}\n")
        if result.get("summary"):
            summary_json = json.dumps(result["summary"], indent=2)
            file.write(f"\n### Iteration Summary\n\n```json\n{summary_json}\n```\n")
        file.write("\n---\n\n")


def append_summary(
    log_path: Path,
    results: list[dict[str, object]],
    best_result: dict[str, object] | None = None,
    fatal_error: str = "",
) -> None:
    with log_path.open("a", encoding="utf-8") as file:
        file.write("## Summary\n\n")
        file.write("| Iteration | Status | Eval Score | Duration |\n")
        file.write("|-----------|--------|------------|----------|\n")
        for result in results:
            file.write(
                f"| {result['iteration']} | {result['status']} | {result['eval_score']} | {result['codex_duration_s']}s |\n"
            )
        if best_result:
            file.write(
                f"\n- **Best Iteration**: {best_result['iteration']} (score: {best_result.get('parsed_score', 'N/A')})\n"
            )
        if fatal_error:
            file.write(f"- **Stopped Early**: {fatal_error}\n")
        file.write(f"\n- **Completed**: {datetime.now().isoformat()}\n")
