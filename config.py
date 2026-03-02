"""Data models for the Claude Code persistent memory system."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MemoryType(str, Enum):
    CODE_PATTERN = "code_pattern"
    ARCHITECTURE_DECISION = "architecture_decision"
    CONVENTION = "convention"
    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    DOCUMENTATION = "documentation"


class MemoryStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class MemorySource(str, Enum):
    AUTO_GENERATED = "auto-generated"
    MANUAL = "manual"
    FROM_CHAT = "from-chat"
    PROPOSED = "proposed"


class MemoryMetadata(BaseModel):
    project: str
    files: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    source: MemorySource = MemorySource.PROPOSED
    git_commit_hash: Optional[str] = None
    git_branch: Optional[str] = None
    stale: bool = False
    last_accessed: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: MemoryType
    content: str
    status: MemoryStatus = MemoryStatus.PROPOSED
    version: int = Field(default=1, ge=1)
    metadata: MemoryMetadata
    embedding: Optional[list[float]] = None

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be empty")
        return v.strip()


class MemorySearchQuery(BaseModel):
    query: str
    project: Optional[str] = None
    type_filter: Optional[list[MemoryType]] = None
    tags: Optional[list[str]] = None
    top_k: int = Field(default=10, ge=1, le=50)
    bypass_prefilter: bool = False


class MemoryUpdateRequest(BaseModel):
    id: str
    new_content: str
    new_tags: Optional[list[str]] = None
    new_confidence: Optional[float] = None


class StalenessResult(BaseModel):
    memory_id: str
    memory_type: MemoryType
    content_preview: str
    files: list[str]
    git_commit_hash: str
    commits_since_capture: int
    changed_files: list[str]


class AppConfig(BaseModel):
    memory_db_path: str = "~/.claude-memory/data/memory.db"
    embeddings_db_path: str = "~/.claude-memory/data/embeddings.db"
    project_path: Optional[str] = None
    git_root: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"
    max_lru_cache_size: int = 100
    proposal_rate_limit_per_minute: int = 10
    proposal_ttl_days: int = 7
