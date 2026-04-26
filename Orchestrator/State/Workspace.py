from __future__ import annotations

import subprocess
from pathlib import Path


def snapshot_worktree(worktree_path: Path, trial_number: int) -> str:
    subprocess.run(
        ["git", "-C", str(worktree_path), "add", "-A"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "--allow-empty", "-m", f"Trial {trial_number} snapshot"],
        capture_output=True,
        text=True,
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def get_head_commit(target_repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def resolve_branch_commit(target_repo: Path, branch_name: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(target_repo), "rev-parse", "--verify", f"refs/heads/{branch_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def list_branches(target_repo: Path, pattern: str) -> list[str]:
    output = subprocess.run(
        ["git", "-C", str(target_repo), "branch", "--format=%(refname:short)", "--list", pattern],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return [line.strip() for line in output.splitlines() if line.strip()]


def delete_branches(target_repo: Path, pattern: str) -> list[str]:
    deleted: list[str] = []
    for name in list_branches(target_repo, pattern):
        result = subprocess.run(
            ["git", "-C", str(target_repo), "branch", "-D", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            deleted.append(name)
            print(f"Deleted branch: {name}")
        else:
            stderr = result.stderr.strip() or "unknown error"
            print(f"Warning: failed to delete branch {name}: {stderr}")
    return deleted


def cleanup_stray_best_branches(
    target_repo: Path,
    best_branch: str,
    stray_branches: list[str] | None = None,
) -> None:
    branches = stray_branches if stray_branches is not None else [
        branch for branch in list_branches(target_repo, "best/*") if branch != best_branch
    ]
    for branch_name in branches:
        result = subprocess.run(
            ["git", "-C", str(target_repo), "branch", "-D", branch_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Deleted stray best branch: {branch_name}")
        else:
            stderr = result.stderr.strip() or "unknown error"
            print(f"Warning: failed to delete stray best branch {branch_name}: {stderr}")


def prune_worktrees(target_repo: Path, *, verbose: bool = False) -> None:
    command = ["git", "-C", str(target_repo), "worktree", "prune"]
    if verbose:
        command.append("--verbose")
    subprocess.run(command, capture_output=not verbose, check=True)


def create_worktree(target_repo: Path, worktree_path: Path, commit: str) -> None:
    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", str(target_repo), "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
        )
    prune_worktrees(target_repo)
    subprocess.run(
        ["git", "-C", str(target_repo), "worktree", "add", "--detach", str(worktree_path), commit],
        capture_output=True,
        text=True,
        check=True,
    )
