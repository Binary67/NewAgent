from pathlib import Path

from Agents.Codex import CodexSessionRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    runner = CodexSessionRunner()
    result = runner.run(
        cwd=PROJECT_ROOT,
        instruction=(
            "Create a file called hello_world.py in the current directory. "
            "It should print 'Hello, World!' when run. Do not ask for any approval."
        ),
    )

    print("--- Response ---")
    print(result.turn_result.response_text)
    print()

    if result.turn_result.commands:
        print("--- Commands ---")
        for cmd in result.turn_result.commands:
            print(f"  {cmd.command}  (status={cmd.status}, exit_code={cmd.exit_code})")
        print()

    if result.turn_result.file_changes:
        print("--- File Changes ---")
        for fc in result.turn_result.file_changes:
            print(f"  {fc.kind}: {fc.path}")
        print()

    if result.turn_result.errors_and_recoveries:
        print("--- Errors ---")
        for err in result.turn_result.errors_and_recoveries:
            print(f"  {err}")
        print()

    print(f"Session log: {result.session_log_path}")


if __name__ == "__main__":
    main()
