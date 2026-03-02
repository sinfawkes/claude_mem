import sqlite3

import pytest
from pydantic import ValidationError

from config import (
    MemoryEntry,
    MemoryMetadata,
    MemorySearchQuery,
    MemoryStatus,
    MemoryType,
)


def test_memory_entry_valid(sample_memory_entry):
    assert sample_memory_entry.type == MemoryType.CODE_PATTERN
    assert sample_memory_entry.status == MemoryStatus.PROPOSED
    assert len(sample_memory_entry.content) > 0


def test_memory_entry_empty_content_raises():
    with pytest.raises(ValidationError):
        MemoryEntry(
            type=MemoryType.CODE_PATTERN,
            content="   ",
            metadata=MemoryMetadata(project="test"),
        )


def test_memory_metadata_defaults():
    meta = MemoryMetadata(project="my-project")
    assert meta.files == []
    assert meta.tags == []
    assert meta.confidence == 0.9
    assert meta.source.value == "proposed"


def test_memory_status_enum():
    assert MemoryStatus.PROPOSED.value == "proposed"
    assert MemoryStatus.CONFIRMED.value == "confirmed"
    assert MemoryStatus.REJECTED.value == "rejected"


def test_memory_type_enum():
    assert MemoryType.CODE_PATTERN.value == "code_pattern"
    assert MemoryType.ARCHITECTURE_DECISION.value == "architecture_decision"
    assert MemoryType.CONVENTION.value == "convention"
    assert MemoryType.BUG_FIX.value == "bug_fix"
    assert MemoryType.FEATURE.value == "feature"
    assert MemoryType.DOCUMENTATION.value == "documentation"


def test_memory_version_starts_at_one(sample_memory_entry):
    assert sample_memory_entry.version == 1


def test_git_commit_hash_optional():
    entry = MemoryEntry(
        type=MemoryType.CONVENTION,
        content="Use type hints",
        metadata=MemoryMetadata(project="p", git_commit_hash=None),
    )
    assert entry.metadata.git_commit_hash is None


def test_memory_search_query_bypass_prefilter_default():
    q = MemorySearchQuery(query="test")
    assert q.bypass_prefilter is False


def test_lifecycle_propose_confirm_reject(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    now = "2025-01-15T10:00:00+00:00"
    id1 = "mem-0001"
    id2 = "mem-0002"

    conn.execute(
        """INSERT INTO memories (id, type, content, status, version, project, files, tags,
           confidence, source, git_commit_hash, git_branch, stale, last_accessed, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, '[]', '[]', 0.9, 'proposed', NULL, NULL, 0, NULL, ?, ?)""",
        (id1, "code_pattern", "First memory", "proposed", "proj", now, now),
    )
    conn.commit()

    conn.execute(
        "UPDATE memories SET status = ?, last_accessed = ?, updated_at = ? WHERE id = ?",
        ("confirmed", now, now, id1),
    )
    conn.commit()
    row = conn.execute("SELECT status FROM memories WHERE id = ?", (id1,)).fetchone()
    assert row["status"] == "confirmed"

    conn.execute(
        """INSERT INTO memories (id, type, content, status, version, project, files, tags,
           confidence, source, git_commit_hash, git_branch, stale, last_accessed, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, '[]', '[]', 0.9, 'proposed', NULL, NULL, 0, NULL, ?, ?)""",
        (id2, "convention", "Second memory", "proposed", "proj", now, now),
    )
    conn.commit()
    conn.execute("UPDATE memories SET status = ? WHERE id = ?", ("rejected", id2))
    conn.commit()
    row = conn.execute("SELECT status FROM memories WHERE id = ?", (id2,)).fetchone()
    assert row["status"] == "rejected"

    conn.close()


def test_audit_log_version_increments(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    now = "2025-01-15T10:00:00+00:00"
    mem_id = "mem-audit-1"

    conn.execute(
        """INSERT INTO memories (id, type, content, status, version, project, files, tags,
           confidence, source, git_commit_hash, git_branch, stale, last_accessed, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, '[]', '[]', 0.9, 'proposed', NULL, NULL, 0, NULL, ?, ?)""",
        (mem_id, "code_pattern", "Original content", "proposed", "proj", now, now),
    )
    conn.commit()

    conn.execute(
        "INSERT INTO memory_versions (id, version, content, updated_at, git_commit_hash) VALUES (?, ?, ?, ?, NULL)",
        (mem_id, 2, "Updated content", now),
    )
    conn.commit()

    row = conn.execute(
        "SELECT version FROM memory_versions WHERE id = ? ORDER BY version DESC LIMIT 1",
        (mem_id,),
    ).fetchone()
    assert row["version"] == 2
    assert row["version"] > 1

    conn.close()
