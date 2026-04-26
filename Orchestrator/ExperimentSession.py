from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from Agents.Codex import CodexSession

from .Evaluation.Evaluation import (
    HIDDEN_EVAL_TOOL,
    build_eval_followup_message,
    build_eval_handler,
    run_requested_eval,
)
from .Learning.Learning import (
    build_summary_request,
    is_experiment_complete,
    parse_experiment_summary,
)


def run_iteration_session(
    *,
    iteration: int,
    agent_worktree: Path,
    eval_worktree: Path,
    role: str,
    eval_command: str,
    eval_repo_path: Path | None,
    eval_overrides: list[str],
    prewarm_command: str,
    prewarm_watch_files: list[str],
    baseline_score: float | None,
    max_eval_calls: int,
    eval_prewarm_state: tuple[tuple[str, bool, int, int], ...],
    maximize: bool,
) -> dict[str, object]:
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
    if protocol_error:
        status = "protocol_error"
    elif codex_failed:
        status = "codex_error"
    else:
        status = "completed"

    return {
        "session_log": str(session_log) if session_log else None,
        "codex_response": codex_response,
        "codex_duration_s": codex_duration,
        "status": status,
        "error": protocol_error or ("" if not codex_failed else codex_response),
        "eval_calls_used": len(trials),
        "summary": iteration_summary,
        "trials": trials,
    }
