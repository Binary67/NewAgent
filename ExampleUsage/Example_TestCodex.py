from pathlib import Path

from Agents.Codex import CodexSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with CodexSession(cwd=PROJECT_ROOT) as session:
        result = session.run_turn(
            "Create a file called hello_world.py in the current directory. "
            "It should print 'Hello, World!' when run. Do not ask for any approval."
        )
        session_log_path = session.session_log_path

    print("--- Response ---")
    print(result.response_text)
    print()

    if result.commands:
        print("--- Commands ---")
        for cmd in result.commands:
            print(f"  {cmd.command}  (status={cmd.status}, exit_code={cmd.exit_code})")
        print()

    if result.file_changes:
        print("--- File Changes ---")
        for fc in result.file_changes:
            print(f"  {fc.kind}: {fc.path}")
        print()

    if result.errors_and_recoveries:
        print("--- Errors ---")
        for err in result.errors_and_recoveries:
            print(f"  {err}")
        print()

    print(f"Session log: {session_log_path}")


if __name__ == "__main__":
    main()
