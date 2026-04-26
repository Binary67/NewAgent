from __future__ import annotations

from pathlib import Path

from Agents.Codex import CodexSession

from .Learning import (
    build_reflection_request,
    choose_reflection_logs,
    parse_reflection_response,
)


def run_reflection(
    results: list[dict[str, object]],
    maximize: bool,
    run_id: str,
    iteration_record_path: Path,
    run_reflection_path: Path,
    experiment_memory_path: Path,
    project_root: Path,
) -> None:
    if not iteration_record_path.exists():
        run_reflection_path.write_text(
            _build_reflection_fallback("No iteration records were produced. Experiment memory left unchanged."),
            encoding="utf-8",
        )
        return

    selected_logs = choose_reflection_logs(results, maximize)
    reflection_request = build_reflection_request(
        run_id,
        iteration_record_path,
        experiment_memory_path,
        selected_logs,
    )

    try:
        with CodexSession(cwd=project_root, role="reflection") as session:
            reflection_turn = session.run_turn(reflection_request)
        run_reflection_text, experiment_memory = parse_reflection_response(reflection_turn.response_text)
    except Exception as exc:
        print(f"Reflection failed: {exc}")
        run_reflection_path.write_text(
            _build_reflection_fallback(f"Reflection failed: {exc}. Experiment memory left unchanged."),
            encoding="utf-8",
        )
        return

    run_reflection_path.write_text(f"{run_reflection_text.rstrip()}\n", encoding="utf-8")
    experiment_memory_path.write_text(f"{experiment_memory.rstrip()}\n", encoding="utf-8")
    print(f"Reflection complete. Run reflection: {run_reflection_path}")


def _build_reflection_fallback(message: str) -> str:
    return (
        "# Run Reflection\n\n"
        "## Patterns That Helped\n"
        "- No reflection output was available.\n\n"
        "## Patterns That Hurt\n"
        f"- {message}\n\n"
        "## Unresolved Questions\n"
        "- Reflection artifacts should be reviewed manually.\n\n"
        "## Memory Updates Applied\n"
        "- Experiment memory was left unchanged.\n"
    )
