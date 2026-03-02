"""
Staleness detection for memory entries anchored to git commit hashes.

A memory is considered stale if any of its referenced files have changed
in the git history since the commit at which the memory was captured.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: str, timeout: int = 10) -> Optional[str]:
    """Run a git command; return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def is_git_repo(path: str) -> bool:
    return _run_git(["rev-parse", "--git-dir"], cwd=path) is not None


def get_current_commit(git_root: str) -> Optional[str]:
    return _run_git(["rev-parse", "HEAD"], cwd=git_root)


def commits_since(git_root: str, since_hash: str, files: list[str]) -> list[str]:
    """
    Return the list of commits that touched any of `files` since `since_hash`.
    Returns an empty list if no changes or if git is unavailable.
    """
    if not files:
        return []

    args = ["log", "--oneline", f"{since_hash}..HEAD", "--", *files]
    output = _run_git(args, cwd=git_root)

    if output is None or output == "":
        return []
    return [line.split(" ", 1)[0] for line in output.splitlines() if line.strip()]


def changed_files_since(git_root: str, since_hash: str, files: list[str]) -> list[str]:
    """
    Return the subset of `files` that have been modified since `since_hash`.
    """
    changed = []
    for file_path in files:
        args = ["log", "--oneline", f"{since_hash}..HEAD", "--", file_path]
        output = _run_git(args, cwd=git_root)
        if output:
            changed.append(file_path)
    return changed


def check_memory_staleness(
    memory_id: str,
    git_commit_hash: Optional[str],
    files: list[str],
    git_root: str,
) -> dict:
    """
    Check whether a single memory entry is stale.

    Returns a dict with:
      - stale (bool): True if any referenced files changed since capture
      - commits_since_capture (int): number of intervening commits on those files
      - changed_files (list[str]): which specific files changed
      - git_unavailable (bool): True if git check could not be performed
    """
    if not git_commit_hash:
        return {
            "memory_id": memory_id,
            "stale": False,
            "commits_since_capture": 0,
            "changed_files": [],
            "git_unavailable": True,
            "reason": "no git_commit_hash stored",
        }

    if not is_git_repo(git_root):
        return {
            "memory_id": memory_id,
            "stale": False,
            "commits_since_capture": 0,
            "changed_files": [],
            "git_unavailable": True,
            "reason": "not a git repository",
        }

    intervening = commits_since(git_root, git_commit_hash, files)
    changed = changed_files_since(git_root, git_commit_hash, files)

    return {
        "memory_id": memory_id,
        "stale": len(changed) > 0,
        "commits_since_capture": len(intervening),
        "changed_files": changed,
        "git_unavailable": False,
        "reason": None,
    }


def check_all_memories_staleness(
    memories: list[dict],
    git_root: str,
) -> list[dict]:
    """
    Batch staleness check for a list of memory dicts.
    Each dict must have keys: id, git_commit_hash, files (JSON string or list).
    """
    import json

    results = []
    for mem in memories:
        files = mem.get("files", [])
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except (json.JSONDecodeError, TypeError):
                files = []

        result = check_memory_staleness(
            memory_id=mem["id"],
            git_commit_hash=mem.get("git_commit_hash"),
            files=files,
            git_root=git_root,
        )
        result["memory_content_preview"] = mem.get("content", "")[:100]
        result["memory_type"] = mem.get("type", "")
        results.append(result)

    return results
