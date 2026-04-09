from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .Agent import CodexAgent, CodexTurnResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "codex_config.toml"


@dataclass(frozen=True)
class CodexSessionRunResult:
    turn_result: CodexTurnResult
    session_log_path: Path | None


def _load_instructions(role: str | None) -> str:
    """Load base and optional role instructions from codex_config.toml."""
    try:
        config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return ""

    parts = []

    base = config.get("instructions", {}).get("base", "").strip()
    if base:
        parts.append(base)

    if role:
        role_text = config.get("roles", {}).get(role, {}).get("instructions", "").strip()
        if role_text:
            parts.append(role_text)

    return "\n\n".join(parts)


def run_codex_session(
    cwd: Path,
    instruction: str,
    *,
    role: str | None = None,
    codex_executable: str | None = None,
    logs_root: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> CodexSessionRunResult:
    preamble = _load_instructions(role)
    full_instruction = f"{preamble}\n\n{instruction}" if preamble else instruction

    agent = CodexAgent(
        codex_executable=codex_executable,
        logs_root=logs_root,
        environment=environment,
    )
    try:
        agent.start_session(str(cwd))
        turn_result = agent.run_instruction(full_instruction)
        session_log_path = agent.session_log_path
        agent.end_session()
    finally:
        agent.close()

    return CodexSessionRunResult(turn_result=turn_result, session_log_path=session_log_path)
