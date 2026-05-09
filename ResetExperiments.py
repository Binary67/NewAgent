from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

from Orchestrator.State.BestState import BEST_STATE_PATH
from Orchestrator.State.Workspace import delete_branches, prune_worktrees

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "CodexConfig.toml"
EXPERIMENT_MEMORY_PATH = PROJECT_ROOT / "Prompts" / "ExperimentMemory.md"

config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
TARGET_REPO = config["Experiment"]["target_repo"]


def reset_experiments(target_repo: str | Path | None = None):
    worktree_count = 0
    log_count = 0
    branch_count = 0
    metadata_removed = False
    learning_file_count = 0
    generated_eval_file_count = 0
    pycache_count = 0

    worktree_dir = PROJECT_ROOT / "Worktrees"
    if worktree_dir.exists():
        worktree_count = sum(1 for p in worktree_dir.iterdir() if p.is_dir())
        shutil.rmtree(worktree_dir)
        print(f"Removed {worktree_count} worktree(s) from {worktree_dir}")
    else:
        print("No worktrees to remove.")

    logs_dir = PROJECT_ROOT / "Logs"
    if logs_dir.exists():
        log_count = sum(1 for p in logs_dir.rglob("*") if p.is_file())
        shutil.rmtree(logs_dir)
        logs_dir.mkdir()
        print(f"Cleared {log_count} log file(s) from {logs_dir}")
    else:
        print("No logs to clear.")

    if EXPERIMENT_MEMORY_PATH.exists():
        EXPERIMENT_MEMORY_PATH.unlink()
        learning_file_count = 1
        print(f"Removed experiment memory: {EXPERIMENT_MEMORY_PATH}")
    else:
        print("No experiment memory to remove.")

    if BEST_STATE_PATH.exists():
        BEST_STATE_PATH.unlink()
        metadata_removed = True
        print(f"Removed best state metadata: {BEST_STATE_PATH}")
    else:
        print("No best state metadata to remove.")

    for pycache_dir in sorted(PROJECT_ROOT.rglob("__pycache__")):
        if pycache_dir.is_dir():
            shutil.rmtree(pycache_dir)
            pycache_count += 1

    if pycache_count:
        print(f"Removed {pycache_count} __pycache__ dir(s) from {PROJECT_ROOT}")
    else:
        print("No __pycache__ directories to remove.")

    if target_repo:
        target = Path(target_repo).resolve()
        safe_target_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", target.name).strip("._") or "target_repo"
        generated_evals_dir = PROJECT_ROOT / "GeneratedEvals"
        target_generated_eval_dir = generated_evals_dir / safe_target_name
        if target_generated_eval_dir.exists():
            generated_eval_file_count = sum(1 for p in target_generated_eval_dir.rglob("*") if p.is_file())
            shutil.rmtree(target_generated_eval_dir)
            print(f"Removed {generated_eval_file_count} generated eval file(s) from {target_generated_eval_dir}")
            if generated_evals_dir.exists() and not any(generated_evals_dir.iterdir()):
                generated_evals_dir.rmdir()
                print(f"Removed empty generated evals directory: {generated_evals_dir}")
        else:
            print(f"No generated evals to remove for {target.name}.")

        prune_worktrees(target, verbose=True)
        print(f"Pruned stale worktree refs in {target}")

        deleted_branches: list[str] = []
        for pattern in ("experiment/iter_*", "best/*"):
            deleted_branches.extend(delete_branches(target, pattern))

        branch_count = len(deleted_branches)
        if branch_count:
            print(f"Deleted {branch_count} branch(es) matching experiment/iter_* and best/*")
        else:
            print("No experiment branches to delete.")

    metadata_count = 1 if metadata_removed else 0
    print(
        f"\nReset complete. Removed {worktree_count} worktree(s), {log_count} log file(s), "
        f"{branch_count} branch(es), {metadata_count} metadata file(s), "
        f"{learning_file_count} learning file(s), "
        f"{generated_eval_file_count} generated eval file(s), "
        f"{pycache_count} __pycache__ dir(s)."
    )


reset_experiments(target_repo=TARGET_REPO)
