from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .Agent import CodexAgent, CodexTurnResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = PROJECT_ROOT / "Prompts"


def _load_instructions(role: str | None) -> str:
    """Load base and optional role instructions from Prompts/ directory."""
    parts = []

    base_path = PROMPTS_DIR / "Base.md"
    if base_path.exists():
        base = base_path.read_text(encoding="utf-8").strip()
        if base:
            parts.append(base)

    if role == "experiment":
        experiment_memory_path = PROMPTS_DIR / "ExperimentMemory.md"
        if experiment_memory_path.exists():
            experiment_memory = experiment_memory_path.read_text(encoding="utf-8").strip()
            if experiment_memory:
                parts.append(experiment_memory)

    if role:
        role_path = PROMPTS_DIR / f"{''.join(part.capitalize() for part in role.split('_'))}.md"
        if role_path.exists():
            role_text = role_path.read_text(encoding="utf-8").strip()
            if role_text:
                parts.append(role_text)

    return "\n\n".join(parts)


def _build_session_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    session_environment = dict(os.environ)
    active_virtual_env = session_environment.get("VIRTUAL_ENV", "")

    if active_virtual_env:
        active_virtual_env_path = Path(active_virtual_env).resolve()
        filtered_path_entries: list[str] = []
        for raw_entry in session_environment.get("PATH", "").split(os.pathsep):
            if not raw_entry:
                continue
            try:
                entry_path = Path(raw_entry).resolve()
            except OSError:
                filtered_path_entries.append(raw_entry)
                continue

            try:
                is_inside_active_env = entry_path.is_relative_to(active_virtual_env_path)
            except AttributeError:
                is_inside_active_env = str(entry_path).startswith(str(active_virtual_env_path))

            if not is_inside_active_env:
                filtered_path_entries.append(raw_entry)

        session_environment["PATH"] = os.pathsep.join(filtered_path_entries)

    if environment:
        session_environment.update(environment)

    for key in ("VIRTUAL_ENV", "PYTHONHOME", "__PYVENV_LAUNCHER__"):
        session_environment.pop(key, None)

    return session_environment


class CodexSession:
    def __init__(
        self,
        cwd: Path,
        *,
        role: str | None = None,
        codex_executable: str | None = None,
        logs_root: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
        tool_handler: Callable[[str, Any], dict[str, Any]] | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._preamble = _load_instructions(role)
        self._agent = CodexAgent(
            codex_executable=codex_executable,
            logs_root=logs_root,
            environment=_build_session_environment(environment),
            tool_handler=tool_handler,
        )
        try:
            self._agent.start_session(str(self._cwd), dynamic_tools=dynamic_tools)
        except Exception:
            self._agent.close()
            raise

    @property
    def session_log_path(self) -> Path | None:
        return self._agent.session_log_path

    def run_turn(self, text: str) -> CodexTurnResult:
        full_instruction = f"{self._preamble}\n\n{text}" if self._preamble else text
        return self._agent.run_instruction(full_instruction)

    def close(self) -> None:
        try:
            self._agent.end_session()
        finally:
            self._agent.close()

    def __enter__(self) -> CodexSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
