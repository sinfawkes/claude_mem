from unittest.mock import MagicMock, patch

import pytest

from staleness import (
    check_all_memories_staleness,
    check_memory_staleness,
    commits_since,
    is_git_repo,
)


def test_not_stale_when_no_commit_hash():
    result = check_memory_staleness(
        memory_id="m1",
        git_commit_hash=None,
        files=["src/main.py"],
        git_root="/tmp/repo",
    )
    assert result["stale"] is False
    assert result["git_unavailable"] is True


def test_not_stale_when_no_file_changes():
    def fake_run_git(args, cwd, timeout=10):
        if "log" in args:
            return ""
        return ".git"

    with patch("staleness._run_git", side_effect=fake_run_git):
        result = check_memory_staleness(
            memory_id="m1",
            git_commit_hash="abc1234",
            files=["src/main.py"],
            git_root="/tmp/repo",
        )
    assert result["stale"] is False
    assert result["git_unavailable"] is False


def test_stale_when_files_changed():
    target_file = "src/battle.py"

    def fake_run_git(args, cwd, timeout=10):
        if "log" in args and target_file in args:
            return "def5678 Update battle logic"
        if "log" in args:
            return ""
        return ".git"

    with patch("staleness._run_git", side_effect=fake_run_git):
        result = check_memory_staleness(
            memory_id="m1",
            git_commit_hash="abc1234",
            files=[target_file],
            git_root="/tmp/repo",
        )
    assert result["stale"] is True
    assert target_file in result["changed_files"]


def test_commits_since_empty_on_no_changes():
    def fake_run_git(args, cwd, timeout=10):
        return ""

    with patch("staleness._run_git", side_effect=fake_run_git):
        out = commits_since("/tmp/repo", "oldhash", ["f1.py"])
    assert out == []


def test_commits_since_parses_hashes():
    def fake_run_git(args, cwd, timeout=10):
        return "aaa1111 fix bug\nbbb2222 add feature\nccc3333 refactor"

    with patch("staleness._run_git", side_effect=fake_run_git):
        out = commits_since("/tmp/repo", "oldhash", ["f1.py"])
    assert out == ["aaa1111", "bbb2222", "ccc3333"]


def test_batch_staleness_empty_hash_graceful():
    memories = [
        {"id": "m1", "git_commit_hash": None, "files": []},
    ]
    results = check_all_memories_staleness(memories, "/tmp/repo")
    assert len(results) == 1
    assert results[0]["stale"] is False
    assert results[0]["git_unavailable"] is True


def test_is_git_repo_false_on_failure():
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stdout = ""

    with patch("subprocess.run", return_value=mock_result):
        assert is_git_repo("/tmp/repo") is False
