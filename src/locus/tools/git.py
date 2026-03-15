from __future__ import annotations

import subprocess


def git_diff(cwd: str) -> str:
    """Show all uncommitted changes (staged + unstaged) in the repo."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout


def git_restore(cwd: str, file: str) -> str:
    """Restore a file to its last committed state."""
    result = subprocess.run(
        ["git", "checkout", "HEAD", "--", file],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return "OK: restored"
