from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

EXPERIMENT_COMPLETE_MARKER = "EXPERIMENT_COMPLETE"
EXPERIMENT_SUMMARY_TAG = "experiment_summary"
RUN_REFLECTION_TAG = "run_reflection"
EXPERIMENT_MEMORY_TAG = "experiment_memory"
EXPERIMENT_MEMORY_CHAR_LIMIT = 2400

DEFAULT_EXPERIMENT_MEMORY = """# Experiment Memory

These are heuristics, not hard rules. Use them to choose a strong first hypothesis and override them when concrete evidence points elsewhere.

## Search Priorities
- Start with the code paths closest to the measured objective.

## Productive Patterns
- Test one cohesive hypothesis per evaluation call.

## Failure Signals
- Broad changes without a score-linked hypothesis usually waste iterations.

## Guardrails
- Keep lessons problem-agnostic and language-agnostic.
"""

_SUMMARY_LIST_LIMITS = {
    "change_summary": 5,
    "success_patterns": 3,
    "failure_patterns": 3,
    "next_directions": 3,
    "memory_candidates": 3,
}
_MEMORY_KINDS = {"search_priority", "productive_pattern", "failure_pattern", "guardrail"}
_MEMORY_SCOPES = {"general", "repo_current"}
_MEMORY_CONFIDENCE = {"low", "medium", "high"}
_RESULT_ASSESSMENTS = {"improved", "not_improved", "inconclusive"}
_MEMORY_SECTIONS = (
    "## Search Priorities",
    "## Productive Patterns",
    "## Failure Signals",
    "## Guardrails",
)
_RUN_REFLECTION_SECTIONS = (
    "## Patterns That Helped",
    "## Patterns That Hurt",
    "## Unresolved Questions",
    "## Memory Updates Applied",
)


def is_experiment_complete(response_text: str) -> bool:
    stripped = response_text.rstrip()
    if not stripped:
        return False
    if not stripped.endswith(EXPERIMENT_COMPLETE_MARKER):
        return False
    return bool(re.search(rf"(?m)^{re.escape(EXPERIMENT_COMPLETE_MARKER)}$", stripped))


def build_summary_request(
    iteration: int,
    baseline_score: float | None,
    best_score: float | None,
    remaining_eval_calls: int,
) -> str:
    baseline_text = "N/A" if baseline_score is None else str(baseline_score)
    best_text = "N/A" if best_score is None else str(best_score)
    return (
        f"Iteration {iteration} experiment phase is complete.\n"
        "Do not modify files, do not run commands, and do not call `run_hidden_eval`.\n"
        "Return exactly one <experiment_summary>...</experiment_summary> block containing valid JSON and no extra text.\n"
        "Keep the response concise and within these limits:\n"
        "- change_summary: max 5 items\n"
        "- success_patterns: max 3 items\n"
        "- failure_patterns: max 3 items\n"
        "- next_directions: max 3 items\n"
        "- memory_candidates: max 3 items\n\n"
        "Known facts for this iteration:\n"
        f"- Baseline score: {baseline_text}\n"
        f"- Best score: {best_text}\n"
        f"- Remaining eval opportunities after this iteration: {remaining_eval_calls}\n\n"
        "JSON schema:\n"
        "{\n"
        '  "main_hypothesis": "string",\n'
        '  "change_summary": ["string"],\n'
        '  "result_assessment": "improved|not_improved|inconclusive",\n'
        '  "success_patterns": ["string"],\n'
        '  "failure_patterns": ["string"],\n'
        '  "next_directions": ["string"],\n'
        '  "memory_candidates": [\n'
        "    {\n"
        '      "lesson": "string",\n'
        '      "kind": "search_priority|productive_pattern|failure_pattern|guardrail",\n'
        '      "scope": "general|repo_current",\n'
        '      "confidence": "low|medium|high"\n'
        "    }\n"
        "  ]\n"
        "}"
    )


def parse_experiment_summary(response_text: str) -> dict[str, object]:
    raw_json = _extract_single_tag_block(response_text, EXPERIMENT_SUMMARY_TAG)
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON inside <{EXPERIMENT_SUMMARY_TAG}>: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Experiment summary must be a JSON object.")

    summary: dict[str, object] = {
        "main_hypothesis": _require_non_empty_string(payload, "main_hypothesis"),
        "change_summary": _require_string_list(payload, "change_summary", _SUMMARY_LIST_LIMITS["change_summary"]),
        "result_assessment": _require_choice(payload, "result_assessment", _RESULT_ASSESSMENTS),
        "success_patterns": _require_string_list(
            payload,
            "success_patterns",
            _SUMMARY_LIST_LIMITS["success_patterns"],
        ),
        "failure_patterns": _require_string_list(
            payload,
            "failure_patterns",
            _SUMMARY_LIST_LIMITS["failure_patterns"],
        ),
        "next_directions": _require_string_list(
            payload,
            "next_directions",
            _SUMMARY_LIST_LIMITS["next_directions"],
        ),
    }

    raw_candidates = payload.get("memory_candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("memory_candidates must be a list.")
    if len(raw_candidates) > _SUMMARY_LIST_LIMITS["memory_candidates"]:
        raise ValueError("memory_candidates exceeds the maximum length of 3.")

    memory_candidates: list[dict[str, str]] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            raise ValueError("Each memory candidate must be an object.")
        memory_candidates.append(
            {
                "lesson": _require_non_empty_string(raw_candidate, "lesson"),
                "kind": _require_choice(raw_candidate, "kind", _MEMORY_KINDS),
                "scope": _require_choice(raw_candidate, "scope", _MEMORY_SCOPES),
                "confidence": _require_choice(raw_candidate, "confidence", _MEMORY_CONFIDENCE),
            }
        )
    summary["memory_candidates"] = memory_candidates
    return summary


def append_iteration_record(record_path: Path, record: dict[str, object]) -> None:
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(record, ensure_ascii=True)}\n")


def list_changed_files(repo_path: Path, base_commit: str, target_commit: str) -> list[str]:
    if not base_commit or not target_commit:
        return []
    output = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", base_commit, target_commit],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in output.splitlines() if line.strip()]


def choose_reflection_logs(results: list[dict[str, object]], maximize: bool) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()

    def maybe_add(path: str | None) -> None:
        if not path or path in seen:
            return
        seen.add(path)
        selected.append(path)

    completed_with_scores = [
        result
        for result in results
        if isinstance(result.get("parsed_score"), (int, float)) and result.get("session_log")
    ]

    if completed_with_scores:
        best_picker = max if maximize else min
        worst_picker = min if maximize else max
        best_result = best_picker(completed_with_scores, key=lambda item: float(item["parsed_score"]))
        worst_result = worst_picker(completed_with_scores, key=lambda item: float(item["parsed_score"]))
        maybe_add(str(best_result.get("session_log")))
        maybe_add(str(worst_result.get("session_log")))

    first_improved = next(
        (
            result
            for result in results
            if result.get("session_log")
            and result.get("baseline_score") is not None
            and result.get("parsed_score") is not None
            and (
                float(result["parsed_score"]) > float(result["baseline_score"])
                if maximize
                else float(result["parsed_score"]) < float(result["baseline_score"])
            )
        ),
        None,
    )
    if first_improved:
        maybe_add(str(first_improved.get("session_log")))

    first_error = next(
        (
            result
            for result in results
            if result.get("session_log")
            and result.get("status") in {"codex_error", "protocol_error", "best_state_error"}
        ),
        None,
    )
    if first_error:
        maybe_add(str(first_error.get("session_log")))

    if results:
        maybe_add(str(results[-1].get("session_log")))

    return selected[:5]


def build_reflection_request(
    run_id: str,
    iteration_record_path: Path,
    experiment_memory_path: Path,
    selected_logs: list[str],
) -> str:
    log_lines = "\n".join(f"- {path}" for path in selected_logs) if selected_logs else "- None selected"
    return (
        f"Run {run_id} is complete.\n"
        "Read the current experiment memory, the iteration records JSONL file, and the selected raw session logs if they add signal.\n"
        "Do not modify files, do not run commands that change state, and do not invent lessons not grounded in the run artifacts.\n"
        "Produce exactly two blocks and no extra text:\n"
        f"1. <{RUN_REFLECTION_TAG}> markdown for the run reflection file\n"
        f"2. <{EXPERIMENT_MEMORY_TAG}> markdown for the updated experiment memory file\n\n"
        "Reflection requirements:\n"
        "- Use the headings: # Run Reflection, ## Patterns That Helped, ## Patterns That Hurt, ## Unresolved Questions, ## Memory Updates Applied\n"
        "- Keep lessons problem-agnostic and language-agnostic.\n"
        "- Treat experiment memory as heuristics, not hard rules.\n"
        "- Exclude one-off diary entries and benchmark-specific hacks.\n"
        "- If there are no durable lessons, leave the memory effectively unchanged and note that decision under Memory Updates Applied.\n\n"
        "Experiment memory requirements:\n"
        "- Maximum 2400 characters total.\n"
        "- Use exactly this structure:\n"
        "# Experiment Memory\n\n"
        "These are heuristics, not hard rules. Use them to choose a strong first hypothesis and override them when concrete evidence points elsewhere.\n\n"
        "## Search Priorities\n"
        "## Productive Patterns\n"
        "## Failure Signals\n"
        "## Guardrails\n"
        "- Maximum 5 bullets under each section.\n\n"
        f"Current experiment memory path: {experiment_memory_path}\n"
        f"Iteration records path: {iteration_record_path}\n"
        "Selected raw session logs:\n"
        f"{log_lines}"
    )


def parse_reflection_response(response_text: str) -> tuple[str, str]:
    run_reflection = _extract_tag_block(response_text, RUN_REFLECTION_TAG).strip()
    experiment_memory = _extract_tag_block(response_text, EXPERIMENT_MEMORY_TAG).strip()
    outside = _strip_tag_block(response_text, RUN_REFLECTION_TAG)
    outside = _strip_tag_block(outside, EXPERIMENT_MEMORY_TAG).strip()
    if outside:
        raise ValueError("Reflection response must contain only the required tag blocks.")
    _validate_run_reflection(run_reflection)
    _validate_experiment_memory(experiment_memory)
    return run_reflection, experiment_memory


def ensure_default_experiment_memory(path: Path) -> None:
    if path.exists():
        return
    path.write_text(f"{DEFAULT_EXPERIMENT_MEMORY.rstrip()}\n", encoding="utf-8")


def _extract_single_tag_block(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL)
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one <{tag}> block.")

    outside = pattern.sub("", text).strip()
    if outside:
        raise ValueError(f"Response must contain only the <{tag}> block.")
    return matches[0]


def _extract_tag_block(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL)
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one <{tag}> block.")
    return matches[0]


def _strip_tag_block(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL)
    return pattern.sub("", text)


def _require_non_empty_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value.strip()


def _require_string_list(payload: dict[str, object], key: str, max_items: int) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list.")
    if len(value) > max_items:
        raise ValueError(f"{key} exceeds the maximum length of {max_items}.")

    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} items must be non-empty strings.")
        items.append(item.strip())
    return items


def _require_choice(payload: dict[str, object], key: str, choices: set[str]) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value not in choices:
        allowed = "|".join(sorted(choices))
        raise ValueError(f"{key} must be one of {allowed}.")
    return value


def _validate_run_reflection(content: str) -> None:
    if not content.startswith("# Run Reflection"):
        raise ValueError("Run reflection must start with '# Run Reflection'.")
    for section in _RUN_REFLECTION_SECTIONS:
        if section not in content:
            raise ValueError(f"Run reflection is missing section: {section}")


def _validate_experiment_memory(content: str) -> None:
    if len(content) > EXPERIMENT_MEMORY_CHAR_LIMIT:
        raise ValueError("Experiment memory exceeds the 2400 character limit.")
    if not content.startswith("# Experiment Memory"):
        raise ValueError("Experiment memory must start with '# Experiment Memory'.")
    required_preamble = (
        "These are heuristics, not hard rules. Use them to choose a strong first hypothesis "
        "and override them when concrete evidence points elsewhere."
    )
    if required_preamble not in content:
        raise ValueError("Experiment memory is missing the required heuristics preamble.")
    for section in _MEMORY_SECTIONS:
        if section not in content:
            raise ValueError(f"Experiment memory is missing section: {section}")
    for section in _MEMORY_SECTIONS:
        body = _section_body(content, section)
        bullets = [line for line in body.splitlines() if line.strip().startswith("- ")]
        if len(bullets) > 5:
            raise ValueError(f"{section} exceeds the maximum of 5 bullets.")


def _section_body(content: str, heading: str) -> str:
    pattern = re.compile(
        rf"{re.escape(heading)}\n(.*?)(?=\n## |\Z)",
        re.DOTALL,
    )
    match = pattern.search(content)
    return "" if match is None else match.group(1)
