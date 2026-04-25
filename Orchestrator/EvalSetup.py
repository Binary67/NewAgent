from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from Agents.Codex import CodexSession

from .Evaluation import apply_eval_overrides, parse_score, run_eval, run_prewarm_command
from .Workspace import create_worktree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "CodexConfig.toml"
GENERATED_EVALS_DIR = PROJECT_ROOT / "GeneratedEvals"
VALIDATION_WORKTREE = PROJECT_ROOT / "Worktrees" / "eval_setup_validation"

PLACEHOLDER_TARGETS = {"D:/HousePricePrediction"}
PLACEHOLDER_EVAL_REPOS = {"D:/HiddenEval"}
MAX_SETUP_TURNS = 30

ASK_USER_CLARIFICATION_TOOL = {
    "name": "ask_user_clarification",
    "description": (
        "Ask the user focused clarification questions about the desired evaluation objective. "
        "Use this whenever the objective, metric, data source, score direction, or constraints are unclear."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Focused questions for the user.",
            },
            "context": {
                "type": "string",
                "description": "Optional short context explaining why the questions matter.",
            },
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
}

SUBMIT_EVAL_SETUP_TOOL = {
    "name": "submit_eval_setup",
    "description": "Submit the generated evaluator configuration for orchestrator validation.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "eval_command": {
                "type": "string",
                "description": "Command that prints the numeric score as the final non-empty stdout line.",
            },
            "eval_strategy": {
                "type": "string",
                "enum": ["maximize", "minimize"],
            },
            "eval_repo": {
                "type": "string",
                "description": "Generated evaluation directory supplied by the orchestrator.",
            },
            "eval_overrides": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Generated evaluator files or glob patterns to copy into eval worktrees.",
            },
            "prewarm_command": {"type": "string"},
            "prewarm_watch_files": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["eval_command", "eval_strategy", "eval_repo", "eval_overrides"],
        "additionalProperties": False,
    },
}


def ensure_evaluator_setup() -> dict[str, Any]:
    existing_config, read_error = _read_existing_config()
    target_repo = _resolve_target_repo(existing_config, read_error)

    if existing_config:
        candidate_config = _build_experiment_config(existing_config, target_repo)
        setup_reason = _setup_reason(candidate_config, read_error)
        if not setup_reason:
            validation_error = validate_experiment_config(candidate_config)
            if not validation_error:
                print(f"Config validated: {CONFIG_PATH}")
                return candidate_config
            setup_reason = f"existing config failed validation: {validation_error}"
    else:
        setup_reason = read_error or "CodexConfig.toml is missing."

    print(f"Starting evaluator setup: {setup_reason}")
    return _run_setup_agent(target_repo, existing_config or {}, setup_reason)


def validate_experiment_config(config: dict[str, Any]) -> str:
    target_repo = Path(str(config.get("target_repo", ""))).expanduser().resolve()
    if not _is_git_repo(target_repo):
        return f"target_repo is not a git repo: {target_repo}"

    eval_strategy = config.get("eval_strategy")
    if eval_strategy not in {"maximize", "minimize"}:
        return "eval_strategy must be exactly 'maximize' or 'minimize'."

    eval_command = str(config.get("eval_command", "")).strip()
    if not eval_command:
        return "eval_command is required."

    eval_repo = str(config.get("eval_repo", "")).strip()
    eval_overrides = config.get("eval_overrides")
    if eval_repo:
        eval_repo_path = Path(eval_repo).expanduser().resolve()
        if not eval_repo_path.is_dir():
            return f"eval_repo does not exist: {eval_repo_path}"
        if not isinstance(eval_overrides, list) or not eval_overrides:
            return "eval_overrides must be a non-empty list when eval_repo is set."
        invalid_patterns = _invalid_override_patterns(eval_overrides)
        if invalid_patterns:
            return f"eval_overrides must be relative patterns inside eval_repo: {invalid_patterns}"
        missing_patterns = [
            pattern for pattern in eval_overrides
            if not any(path.is_file() for path in eval_repo_path.glob(str(pattern)))
        ]
        if missing_patterns:
            return f"eval_overrides matched no files: {missing_patterns}"
    else:
        eval_repo_path = None
        if eval_overrides not in (None, []):
            return "eval_overrides must be empty when eval_repo is empty."

    VALIDATION_WORKTREE.parent.mkdir(parents=True, exist_ok=True)
    try:
        create_worktree(target_repo, VALIDATION_WORKTREE, "HEAD")
    except Exception as exc:
        return f"failed to create validation worktree: {exc}"

    try:
        if eval_repo_path:
            apply_eval_overrides(eval_repo_path, VALIDATION_WORKTREE, [str(pattern) for pattern in eval_overrides])

        prewarm_command = str(config.get("prewarm_command", "")).strip()
        if prewarm_command:
            prewarm_ok, prewarm_error = run_prewarm_command(
                VALIDATION_WORKTREE,
                prewarm_command,
                action="Validating eval prewarm",
            )
            if not prewarm_ok:
                return prewarm_error

        stdout, error = run_eval(eval_command, VALIDATION_WORKTREE)
        if error:
            return f"eval_command failed: {error}"
        parsed_score = parse_score(stdout)
        if parsed_score is None:
            return f"eval_command did not print a numeric final line. Output:\n{stdout}"
        print(f"Evaluator validation score: {parsed_score}")
        return ""
    finally:
        _remove_validation_worktree(target_repo)


def _run_setup_agent(
    target_repo: Path,
    existing_config: dict[str, Any],
    setup_reason: str,
) -> dict[str, Any]:
    generated_eval_dir = GENERATED_EVALS_DIR / _safe_repo_name(target_repo)
    generated_eval_dir.mkdir(parents=True, exist_ok=True)
    setup_state: dict[str, Any] = {"submission": None}

    def setup_tool_handler(tool_name: str, arguments: Any) -> dict[str, Any]:
        parsed_arguments = _parse_tool_arguments(arguments)
        if tool_name == "ask_user_clarification":
            return _handle_user_clarification(parsed_arguments)
        if tool_name == "submit_eval_setup":
            setup_state["submission"] = parsed_arguments
            return {
                "success": True,
                "contentItems": [{"type": "inputText", "text": (
                    "Evaluator setup submitted for validation. End this turn and wait for the validation result."
                )}],
            }
        return {
            "success": False,
            "contentItems": [{"type": "inputText", "text": f"Unknown setup tool: {tool_name}"}],
        }

    initial_prompt = _build_setup_prompt(target_repo, generated_eval_dir, setup_reason, existing_config)
    with CodexSession(
        cwd=PROJECT_ROOT,
        role="eval_setup",
        dynamic_tools=[ASK_USER_CLARIFICATION_TOOL, SUBMIT_EVAL_SETUP_TOOL],
        tool_handler=setup_tool_handler,
    ) as session:
        turn_input = initial_prompt
        for _ in range(MAX_SETUP_TURNS):
            setup_state["submission"] = None
            session.run_turn(turn_input)
            submission = setup_state["submission"]
            if submission is None:
                turn_input = (
                    "You ended the turn without submitting evaluator setup. "
                    "Ask more clarification if needed, otherwise generate the evaluator files and call "
                    "`submit_eval_setup`."
                )
                continue

            candidate_config, config_error = _config_from_submission(
                submission,
                target_repo,
                generated_eval_dir,
                existing_config,
            )
            if config_error:
                turn_input = (
                    "Submitted evaluator setup was rejected before validation:\n"
                    f"{config_error}\n\n"
                    "Fix the setup or ask the user for clarification, then submit again."
                )
                continue

            validation_error = validate_experiment_config(candidate_config)
            if validation_error:
                turn_input = (
                    "Evaluator validation failed:\n"
                    f"{validation_error}\n\n"
                    "Inspect the generated evaluator files and target repo, fix the evaluator or ask the user "
                    "for clarification, then call `submit_eval_setup` again."
                )
                continue

            _write_experiment_config(candidate_config)
            print(f"Wrote evaluator config: {CONFIG_PATH}")
            return candidate_config

    raise RuntimeError("Evaluator setup did not complete within the maximum number of setup turns.")


def _read_existing_config() -> tuple[dict[str, Any] | None, str]:
    if not CONFIG_PATH.exists():
        return None, "CodexConfig.toml is missing."

    try:
        raw_config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return None, f"CodexConfig.toml could not be parsed: {exc}"

    experiment = raw_config.get("Experiment")
    if not isinstance(experiment, dict):
        return None, "CodexConfig.toml does not contain an [Experiment] table."
    return experiment, ""


def _resolve_target_repo(existing_config: dict[str, Any] | None, read_error: str) -> Path:
    raw_target = ""
    if existing_config:
        raw_target_value = existing_config.get("target_repo", "")
        raw_target = str(raw_target_value).strip() if raw_target_value is not None else ""

    if raw_target and raw_target not in PLACEHOLDER_TARGETS:
        target_repo = Path(raw_target).expanduser().resolve()
        if _is_git_repo(target_repo):
            return target_repo
        print(f"Configured target_repo is not a git repo: {target_repo}")
    elif read_error:
        print(read_error)
    else:
        print("Configured target_repo is missing or uses a placeholder value.")

    while True:
        try:
            answer = input("Target repo path: ").strip()
        except EOFError as exc:
            raise RuntimeError("Target repo path is required for evaluator setup.") from exc

        if not answer:
            print("Please provide a target repo path.")
            continue

        target_repo = Path(answer).expanduser().resolve()
        if _is_git_repo(target_repo):
            return target_repo
        print(f"Not a git repo: {target_repo}")


def _setup_reason(config: dict[str, Any], read_error: str) -> str:
    if read_error:
        return read_error

    target_repo = str(config.get("target_repo", "")).strip()
    if not target_repo or target_repo in PLACEHOLDER_TARGETS:
        return "target_repo is missing or uses a placeholder value."

    eval_repo = str(config.get("eval_repo", "")).strip()
    if eval_repo in PLACEHOLDER_EVAL_REPOS:
        return "eval_repo uses a placeholder value."

    if not str(config.get("eval_command", "")).strip():
        return "eval_command is missing."

    if config.get("eval_strategy") not in {"maximize", "minimize"}:
        return "eval_strategy must be exactly 'maximize' or 'minimize'."

    eval_overrides = config.get("eval_overrides")
    if eval_repo and (not isinstance(eval_overrides, list) or not eval_overrides):
        return "eval_overrides must be a non-empty list when eval_repo is set."

    return ""


def _build_experiment_config(raw_config: dict[str, Any], target_repo: Path) -> dict[str, Any]:
    return {
        "target_repo": str(target_repo),
        "eval_command": str(raw_config.get("eval_command", "")).strip(),
        "eval_strategy": str(raw_config.get("eval_strategy", "")).strip(),
        "num_iterations": _positive_int(raw_config.get("num_iterations"), 1),
        "max_eval_calls": _positive_int(raw_config.get("max_eval_calls"), 3),
        "role": str(raw_config.get("role", "experiment")).strip() or "experiment",
        "eval_repo": str(raw_config.get("eval_repo", "")).strip(),
        "eval_overrides": _string_list(raw_config.get("eval_overrides")),
        "prewarm_command": str(raw_config.get("prewarm_command", "")).strip(),
        "prewarm_watch_files": _string_list(raw_config.get("prewarm_watch_files")),
    }


def _config_from_submission(
    submission: dict[str, Any],
    target_repo: Path,
    generated_eval_dir: Path,
    existing_config: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    submitted_eval_repo = str(submission.get("eval_repo", "")).strip()
    eval_repo = Path(submitted_eval_repo or generated_eval_dir).expanduser().resolve()
    expected_eval_repo = generated_eval_dir.resolve()
    if eval_repo != expected_eval_repo:
        return {}, f"eval_repo must be the generated evaluation directory: {expected_eval_repo}"

    eval_overrides = _string_list(submission.get("eval_overrides"))
    if not eval_overrides:
        return {}, "eval_overrides must list at least one generated evaluator file or glob pattern."
    invalid_patterns = _invalid_override_patterns(eval_overrides)
    if invalid_patterns:
        return {}, f"eval_overrides must be relative patterns inside eval_repo: {invalid_patterns}"

    config = _build_experiment_config(existing_config, target_repo)
    config.update(
        {
            "target_repo": str(target_repo),
            "eval_command": str(submission.get("eval_command", "")).strip(),
            "eval_strategy": str(submission.get("eval_strategy", "")).strip(),
            "eval_repo": str(expected_eval_repo),
            "eval_overrides": eval_overrides,
            "prewarm_command": str(submission.get("prewarm_command", "")).strip(),
            "prewarm_watch_files": _string_list(submission.get("prewarm_watch_files")),
        }
    )
    return config, ""


def _handle_user_clarification(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_questions = arguments.get("questions", [])
    questions = [str(question).strip() for question in raw_questions if str(question).strip()]
    if not questions:
        return {
            "success": False,
            "contentItems": [{"type": "inputText", "text": "questions must contain at least one question."}],
        }

    context = str(arguments.get("context", "")).strip()
    print("\nEvaluator setup needs clarification.")
    if context:
        print(context)

    answers = []
    for index, question in enumerate(questions, start=1):
        print(f"{index}. {question}")
        try:
            answer = input("Answer: ").strip()
        except EOFError:
            answer = ""
        answers.append({"question": question, "answer": answer})

    return {
        "success": True,
        "contentItems": [{"type": "inputText", "text": json.dumps({"answers": answers}, indent=2)}],
    }


def _build_setup_prompt(
    target_repo: Path,
    generated_eval_dir: Path,
    setup_reason: str,
    existing_config: dict[str, Any],
) -> str:
    existing_config_text = json.dumps(existing_config, indent=2, default=str) if existing_config else "{}"
    return (
        "Set up an evaluator for this target repo.\n\n"
        f"Setup reason: {setup_reason}\n"
        f"Target repo: {target_repo}\n"
        f"Generated evaluation directory: {generated_eval_dir}\n"
        f"Existing config values, if any:\n{existing_config_text}\n\n"
        "Requirements:\n"
        "- You may read the target repo.\n"
        "- You must not modify the target repo or CodexConfig.toml.\n"
        "- Write generated evaluator artifacts only inside the generated evaluation directory.\n"
        "- Ask the user clarification questions until you understand the desired objective and score direction.\n"
        "- Submit eval_repo exactly as the generated evaluation directory.\n"
        "- Submit eval_overrides as paths or glob patterns relative to eval_repo.\n"
        "- Submit an eval_command that runs from the eval worktree and prints a numeric score as the final "
        "non-empty stdout line.\n"
        "- Use `{eval_worktree}` in eval_command when the command needs the eval worktree path.\n"
    )


def _write_experiment_config(config: dict[str, Any]) -> None:
    lines = [
        "[Experiment]",
        f"target_repo = {_toml_string(config['target_repo'])}",
        f"eval_command = {_toml_string(config['eval_command'])}",
        f"eval_strategy = {_toml_string(config['eval_strategy'])}",
        f"num_iterations = {int(config['num_iterations'])}",
        f"max_eval_calls = {int(config['max_eval_calls'])}",
        f"role = {_toml_string(config['role'])}",
        f"eval_repo = {_toml_string(config['eval_repo'])}",
        f"eval_overrides = {_toml_string_list(config['eval_overrides'])}",
    ]

    if config.get("prewarm_command"):
        lines.append(f"prewarm_command = {_toml_string(config['prewarm_command'])}")
    if config.get("prewarm_watch_files"):
        lines.append(f"prewarm_watch_files = {_toml_string_list(config['prewarm_watch_files'])}")

    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_git_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _remove_validation_worktree(target_repo: Path) -> None:
    if not VALIDATION_WORKTREE.exists():
        return
    result = subprocess.run(
        ["git", "-C", str(target_repo), "worktree", "remove", "--force", str(VALIDATION_WORKTREE)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and VALIDATION_WORKTREE.exists():
        shutil.rmtree(VALIDATION_WORKTREE, ignore_errors=True)


def _safe_repo_name(target_repo: Path) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", target_repo.name).strip("._")
    return safe_name or "target_repo"


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _invalid_override_patterns(patterns: list[Any]) -> list[str]:
    invalid_patterns = []
    for pattern in patterns:
        pattern_path = Path(str(pattern))
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            invalid_patterns.append(str(pattern))
    return invalid_patterns


def _toml_string(value: Any) -> str:
    return json.dumps(str(value))


def _toml_string_list(values: Any) -> str:
    return "[" + ", ".join(_toml_string(value) for value in _string_list(values)) + "]"
