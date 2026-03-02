import sqlite3

import pytest

from config import MemoryEntry, MemoryMetadata, MemorySource, MemoryStatus, MemoryType

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    version INTEGER NOT NULL DEFAULT 1,
    project TEXT,
    files TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.9,
    source TEXT DEFAULT 'proposed',
    git_commit_hash TEXT,
    git_branch TEXT,
    stale INTEGER DEFAULT 0,
    last_accessed TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_project_type_status
    ON memories(project, type, status);

CREATE INDEX IF NOT EXISTS idx_memories_status
    ON memories(status);

CREATE TABLE IF NOT EXISTS memory_versions (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    git_commit_hash TEXT,
    PRIMARY KEY (id, version)
);
"""


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def sample_memory_entry():
    return MemoryEntry(
        type=MemoryType.CODE_PATTERN,
        content="Use pytest fixtures for isolated test setup",
        status=MemoryStatus.PROPOSED,
        metadata=MemoryMetadata(project="test-project"),
    )


@pytest.fixture
def confirmed_memory_dict():
    return {
        "id": "abc12345-0000-0000-0000-000000000001",
        "type": "code_pattern",
        "content": "Always validate input before processing",
        "status": "confirmed",
        "version": 1,
        "project": "my-app",
        "files": ["src/main.py", "src/utils.py"],
        "tags": ["validation", "python"],
        "confidence": 0.95,
        "source": "manual",
        "git_commit_hash": "a1b2c3d4",
        "git_branch": "main",
        "stale": False,
        "last_accessed": "2025-01-15T10:00:00+00:00",
        "created_at": "2025-01-10T09:00:00+00:00",
        "updated_at": "2025-01-15T10:00:00+00:00",
    }
