from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# === Configure this (same target repo as Main.py) ===
TARGET_REPO = "CHANGE_ME"

PROJECT_ROOT = Path(__file__).resolve().parent


def reset_experiments(target_repo: str | Path | None = None):
    worktree_dir = PROJECT_ROOT / "Worktrees"
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir)
        print(f"Removed: {worktree_dir}")
    else:
        print("No worktrees to remove.")

    logs_dir = PROJECT_ROOT / "Logs"
    if logs_dir.exists():
        shutil.rmtree(logs_dir)
        logs_dir.mkdir()
        print(f"Cleared: {logs_dir}")
    else:
        print("No logs to clear.")

    if target_repo and target_repo != "CHANGE_ME":
        target = Path(target_repo).resolve()
        subprocess.run(["git", "-C", str(target), "worktree", "prune"], check=True)
        print(f"Pruned worktree refs: {target}")

        for pattern in ("experiment/iter_*", "best/*"):
            branch_output = subprocess.run(
                ["git", "-C", str(target), "branch", "--list", pattern],
                capture_output=True, text=True,
            ).stdout.strip()
            for line in branch_output.splitlines():
                name = line.strip().removeprefix("* ")
                if name:
                    subprocess.run(
                        ["git", "-C", str(target), "branch", "-D", name],
                        capture_output=True,
                    )
                    print(f"Deleted branch: {name}")

    print("\nReset complete.")


reset_experiments(target_repo=TARGET_REPO)
