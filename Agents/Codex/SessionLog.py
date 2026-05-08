from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class CommandLogEntry:
    command: str
    status: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output: str = ""


@dataclass(frozen=True)
class FileChangeLogEntry:
    path: str
    kind: str | None = None
    diff: str = ""


@dataclass(frozen=True)
class TurnLogEntry:
    user_request: str
    codex_response: str = ""
    commands: list[CommandLogEntry] = field(default_factory=list)
    file_changes: list[FileChangeLogEntry] = field(default_factory=list)
    errors_and_recoveries: list[str] = field(default_factory=list)


class CodexSessionLog:
    def __init__(self, logs_root: Path | str | None = None) -> None:
        self._logs_root = Path(logs_root) if logs_root is not None else self._default_logs_root()
        self._logs_root.mkdir(parents=True, exist_ok=True)
        self._thread_paths: dict[str, Path] = {}
        self._written_paths: set[Path] = set()

    @property
    def logs_root(self) -> Path:
        return self._logs_root

    def path_for_thread(self, thread_id: str) -> Path:
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string.")

        path = self._thread_paths.get(thread_id)
        if path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._logs_root / f"codex_session_{timestamp}.md"
            self._thread_paths[thread_id] = path
        return path

    def append_session_started(self, thread_id: str, cwd: str | None) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                ("Session Started", self._describe_session(cwd)),
                ("Thread Id", thread_id),
            ],
        )

    def append_session_resumed(self, thread_id: str, cwd: str | None) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                ("Session Resumed", self._describe_session(cwd)),
                ("Thread Id", thread_id),
            ],
        )

    def append_turn_started(self, thread_id: str, user_request: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("User Request", user_request)],
        )

    def append_response_snapshot(self, thread_id: str, response_text: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("Codex Response", response_text)],
        )

    def append_command_completed(self, thread_id: str, command: CommandLogEntry) -> Path:
        status_line = command.status or "unknown"
        if command.exit_code is not None:
            status_line = f"{status_line}; exit_code={command.exit_code}"

        sections = [
            ("Commands Run", command.command),
            ("Command Status", status_line),
        ]
        if command.duration_ms is not None:
            sections.append(("Command Duration Ms", str(command.duration_ms)))
        command_failed = (
            command.status in {"failed", "declined"}
            or (command.exit_code is not None and command.exit_code != 0)
        )
        if command_failed and command.output.strip():
            sections.append(self._multi_line_section("Command Output", command.output))

        return self._append_sections(self.path_for_thread(thread_id), sections)

    def append_tool_call(self, thread_id: str, tool_name: str, success: bool, result_text: str) -> Path:
        sections: list[tuple[str, str]] = [
            ("Tool Call", tool_name),
            ("Tool Success", str(success)),
        ]
        if result_text.strip():
            sections.append(self._multi_line_section("Tool Result", result_text))
        return self._append_sections(self.path_for_thread(thread_id), sections)

    def append_turn_finished(self, thread_id: str, turn: TurnLogEntry, status: str) -> Path:
        work_summary = f"Ran {len(turn.commands)} command(s)."
        sections: list[tuple[str, str]] = [
            ("Turn Status", status),
            ("Work Performed", work_summary),
        ]

        if not turn.commands:
            sections.append(("Commands Run", "None"))

        if turn.errors_and_recoveries:
            errors_text = "\n".join(f"- {entry}" for entry in turn.errors_and_recoveries)
            sections.append(self._multi_line_section("Errors And Recoveries", errors_text))
        else:
            sections.append(("Errors And Recoveries", "None"))

        return self._append_sections(self.path_for_thread(thread_id), sections)

    def _append_sections(self, path: Path, sections: list[tuple[str, str]]) -> Path:
        with path.open("a", encoding="utf-8") as handle:
            if path in self._written_paths:
                handle.write("\n")
            else:
                self._written_paths.add(path)
            for title, content in sections:
                if "\n" in content:
                    handle.write(f"[{title}]:\n{content.rstrip()}\n")
                else:
                    handle.write(f"[{title}]: {content}\n")
        return path

    def _describe_session(self, cwd: str | None) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        return f"{timestamp}; cwd={cwd or '(unknown)'}"

    def _multi_line_section(self, title: str, content: str) -> tuple[str, str]:
        normalized = content.strip() or "(empty)"
        if "\n" not in normalized:
            normalized = f"{normalized}\n"
        return title, normalized

    def _default_logs_root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "Logs"
