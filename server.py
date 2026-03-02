"""FastMCP server for the Claude Code persistent memory system."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from fastmcp import FastMCP

from config import AppConfig, MemorySource, MemoryStatus, MemoryType
from fetch_cache import MemoryFetchCache
from knowledge_graph import KnowledgeGraph
import vector_store

load_dotenv()

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


def _load_config() -> AppConfig:
    """Load configuration from environment variables."""
    memory_db = os.environ.get("MEMORY_DB_PATH", "~/.claude-memory/data/memory.db")
    embeddings_db = os.environ.get("EMBEDDINGS_DB_PATH", "~/.claude-memory/data/embeddings.db")
    project_path = os.environ.get("PROJECT_PATH")
    git_root = os.environ.get("GIT_ROOT")
    embedding_model = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    max_lru = int(os.environ.get("MAX_LRU_CACHE_SIZE", "100"))
    rate_limit = int(os.environ.get("PROPOSAL_RATE_LIMIT_PER_MINUTE", "10"))
    ttl_days = int(os.environ.get("PROPOSAL_TTL_DAYS", "7"))

    return AppConfig(
        memory_db_path=os.path.expanduser(memory_db),
        embeddings_db_path=os.path.expanduser(embeddings_db),
        project_path=os.path.expanduser(project_path) if project_path else None,
        git_root=os.path.expanduser(git_root) if git_root else None,
        embedding_model=embedding_model,
        max_lru_cache_size=max_lru,
        proposal_rate_limit_per_minute=rate_limit,
        proposal_ttl_days=ttl_days,
    )


def _ensure_db_dir(path: str) -> None:
    """Create parent directories for the database path if they do not exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _init_db(conn: sqlite3.Connection) -> None:
    """Initialize the database schema."""
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def _get_conn(config: AppConfig) -> sqlite3.Connection:
    """Get a connection to the memory database."""
    _ensure_db_dir(config.memory_db_path)
    conn = sqlite3.connect(config.memory_db_path)
    conn.row_factory = sqlite3.Row
    return conn


mcp = FastMCP("Claude Memory")
config = _load_config()
conn = _get_conn(config)
_init_db(conn)

# Derive embeddings DB path: same directory as memory DB, named project_embeddings.db
# unless explicitly set via EMBEDDINGS_DB_PATH
_embeddings_db_path: str | None = None
_raw_embeddings_env = os.environ.get("EMBEDDINGS_DB_PATH", "")
if _raw_embeddings_env:
    _embeddings_db_path = os.path.expanduser(_raw_embeddings_env)
else:
    _embeddings_db_path = os.path.join(
        os.path.dirname(config.memory_db_path), "project_embeddings.db"
    )

vector_store.init_db(_embeddings_db_path)
logger.info("Embeddings DB: %s", _embeddings_db_path)

graph_path = os.path.join(os.path.dirname(config.memory_db_path), "knowledge_graph.pkl")
fetch_cache = MemoryFetchCache(
    config.memory_db_path,
    max_lru_size=config.max_lru_cache_size,
    embeddings_db_path=_embeddings_db_path,
    embedding_model=config.embedding_model,
)
knowledge_graph = KnowledgeGraph(graph_path)

_proposal_timestamps: deque[float] = deque(maxlen=config.proposal_rate_limit_per_minute * 2)


def _check_proposal_rate_limit() -> bool:
    """Return True if under rate limit, False if exceeded."""
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - 60
    while _proposal_timestamps and _proposal_timestamps[0] < cutoff:
        _proposal_timestamps.popleft()
    if len(_proposal_timestamps) >= config.proposal_rate_limit_per_minute:
        return False
    _proposal_timestamps.append(now)
    return True


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a dictionary."""
    d = dict(row)
    if "files" in d and isinstance(d["files"], str):
        d["files"] = json.loads(d["files"]) if d["files"] else []
    if "tags" in d and isinstance(d["tags"], str):
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    if "stale" in d and isinstance(d["stale"], int):
        d["stale"] = bool(d["stale"])
    return d


def _normalize_memory_type(raw: str) -> MemoryType:
    """
    Map any type string to a valid MemoryType using exact match, then prefix/keyword
    heuristics, then falling back to CODE_PATTERN. Never raises.
    """
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")

    # Exact match first
    if normalized in MemoryType._value2member_map_:
        return MemoryType(normalized)

    # Keyword heuristics — order matters (more specific first)
    if any(k in normalized for k in ("arch", "design_decision", "design_choice")):
        return MemoryType.ARCHITECTURE_DECISION
    if any(k in normalized for k in ("bug", "fix", "patch", "hotfix")):
        return MemoryType.BUG_FIX
    if any(k in normalized for k in ("doc", "note", "comment", "readme", "guide")):
        return MemoryType.DOCUMENTATION
    if any(k in normalized for k in ("feature", "capability", "functionality")):
        return MemoryType.FEATURE
    if any(k in normalized for k in ("convention", "style", "guideline", "standard", "rule")):
        return MemoryType.CONVENTION
    # Anything starting with "code_" or containing "pattern" or "implementation"
    if normalized.startswith("code_") or any(k in normalized for k in ("pattern", "implementation", "structure")):
        return MemoryType.CODE_PATTERN

    logger.warning("Unknown memory type '%s', defaulting to code_pattern", raw)
    return MemoryType.CODE_PATTERN


@mcp.tool
async def memory_propose(
    type: str,
    content: str,
    project: str,
    files: list[str] | None = None,
    tags: list[str] | None = None,
    confidence: float = 0.9,
    source: str = "proposed",
    git_commit_hash: str | None = None,
    git_branch: str | None = None,
) -> dict:
    """
    Propose a new memory for later confirmation.
    Returns the memory id and status. Use memory_confirm(id) to add to the search index.
    """
    if not _check_proposal_rate_limit():
        return {
            "id": "",
            "status": "proposed",
            "message": "Proposal rate limit exceeded. Try again later.",
        }

    memory_type = _normalize_memory_type(type)
    memory_source = MemorySource(source) if source in MemorySource._value2member_map_ else MemorySource.PROPOSED
    now = datetime.now(timezone.utc).isoformat()
    memory_id = str(__import__("uuid").uuid4())

    files_json = json.dumps(files or [])
    tags_json = json.dumps(tags or [])

    conn.execute(
        """
        INSERT INTO memories (
            id, type, content, status, version, project, files, tags,
            confidence, source, git_commit_hash, git_branch, stale,
            last_accessed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
        """,
        (
            memory_id,
            memory_type.value,
            content.strip(),
            MemoryStatus.PROPOSED.value,
            project,
            files_json,
            tags_json,
            confidence,
            memory_source.value,
            git_commit_hash,
            git_branch,
            now,
            now,
        ),
    )
    conn.commit()

    return {
        "id": memory_id,
        "status": "proposed",
        "message": "Memory proposed. Use memory_confirm(id) to add to the search index.",
    }


@mcp.tool
async def memory_confirm(id: str) -> dict:
    """
    Confirm a proposed memory, moving it to confirmed status and adding it to the search index.
    """
    cur = conn.execute(
        "SELECT id, content, version FROM memories WHERE id = ? AND status = ?",
        (id, MemoryStatus.PROPOSED.value),
    )
    row = cur.fetchone()
    if not row:
        return {"id": id, "status": "proposed", "message": "Memory not found or already confirmed/rejected."}

    now = datetime.now(timezone.utc).isoformat()
    version = row["version"]
    content = row["content"]

    conn.execute(
        "UPDATE memories SET status = ?, last_accessed = ?, updated_at = ? WHERE id = ?",
        (MemoryStatus.CONFIRMED.value, now, now, id),
    )
    conn.execute(
        "INSERT INTO memory_versions (id, version, content, updated_at, git_commit_hash) VALUES (?, ?, ?, ?, NULL)",
        (id, version, content, now),
    )
    conn.commit()

    fetch_cache.invalidate(id)

    # Generate and store the embedding for semantic search
    try:
        vector_store.add_embedding(_embeddings_db_path, id, content, config.embedding_model)
    except Exception as exc:
        logger.warning("Could not generate embedding for %s: %s", id, exc)

    files_raw = conn.execute("SELECT files FROM memories WHERE id = ?", (id,)).fetchone()
    files = json.loads(files_raw["files"]) if files_raw and files_raw["files"] else []
    if files:
        knowledge_graph.link_memory_to_files(id, files)
        knowledge_graph.save()

    return {"id": id, "status": "confirmed", "version": version}


@mcp.tool
async def memory_reject(id: str) -> dict:
    """Reject a proposed memory."""
    conn.execute("UPDATE memories SET status = ? WHERE id = ?", (MemoryStatus.REJECTED.value, id))
    conn.commit()
    return {"id": id, "status": "rejected"}


@mcp.tool
async def memory_search(
    query: str,
    project: str | None = None,
    type_filter: str | None = None,
    tags: str | None = None,
    top_k: int = 10,
    bypass_prefilter: bool = False,
) -> list[dict]:
    """
    Search memories by query. Uses tiered fetch: L2 SQL pre-filter, L3 text/vector search.
    Only returns confirmed entries.
    """
    type_list = [t.strip() for t in (type_filter or "").split(",") if t.strip()] or None
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    rows = fetch_cache.search(
        query=query,
        project=project,
        type_filter=type_list,
        tags=tag_list,
        top_k=top_k,
        bypass_prefilter=bypass_prefilter,
    )
    return [_row_to_dict(r) for r in rows]


@mcp.tool
async def memory_get(id: str) -> dict | None:
    """Fetch a single memory by id."""
    cur = conn.execute("SELECT * FROM memories WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_dict(row)


@mcp.tool
async def memory_update(
    id: str,
    new_content: str,
    new_tags: list[str] | None = None,
    new_confidence: float | None = None,
) -> dict:
    """
    Update a memory. Saves old version to memory_versions, increments version.
    If entry was confirmed, status is set back to proposed (re-confirmation required).
    """
    cur = conn.execute("SELECT * FROM memories WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        return {"id": id, "status": "error", "message": "Memory not found."}

    old_version = row["version"]
    old_content = row["content"]
    old_status = row["status"]
    now = datetime.now(timezone.utc).isoformat()
    new_version = old_version + 1

    conn.execute(
        "INSERT INTO memory_versions (id, version, content, updated_at, git_commit_hash) VALUES (?, ?, ?, ?, NULL)",
        (id, old_version, old_content, now),
    )

    updates = ["content = ?", "version = ?", "updated_at = ?"]
    params: list = [new_content.strip(), new_version, now]

    if new_tags is not None:
        updates.append("tags = ?")
        params.append(json.dumps(new_tags))

    if new_confidence is not None:
        updates.append("confidence = ?")
        params.append(new_confidence)

    if old_status == MemoryStatus.CONFIRMED.value:
        updates.append("status = ?")
        params.append(MemoryStatus.PROPOSED.value)

    params.append(id)
    conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    fetch_cache.invalidate(id)

    # Revert-to-proposed removes the embedding; it will be re-generated on next confirm
    if old_status == MemoryStatus.CONFIRMED.value:
        try:
            vector_store.remove_embedding(_embeddings_db_path, id)
        except Exception as exc:
            logger.warning("Could not remove embedding for %s: %s", id, exc)

    return {
        "id": id,
        "status": MemoryStatus.PROPOSED.value if old_status == MemoryStatus.CONFIRMED.value else old_status,
        "version": new_version,
    }


@mcp.tool
async def memory_delete(id: str) -> dict:
    """Hard delete a memory from the memories table. memory_versions is retained for audit."""
    conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    conn.commit()
    try:
        vector_store.remove_embedding(_embeddings_db_path, id)
    except Exception as exc:
        logger.warning("Could not remove embedding for %s: %s", id, exc)
    return {"id": id, "deleted": True}


@mcp.tool
async def memory_check_staleness() -> dict:
    """Check memories for staleness based on git history. Phase 1 returns placeholder."""
    return {
        "message": "Staleness checking requires git integration (Phase 6)",
        "stale_count": 0,
    }


@mcp.tool
async def memory_refresh(id: str) -> dict:
    """
    Refresh a memory by re-proposing it with the same content and new metadata.
    Returns the new memory id and original id.
    """
    cur = conn.execute(
        "SELECT id, type, content, project, files, tags, confidence, source, git_commit_hash, git_branch FROM memories WHERE id = ?",
        (id,),
    )
    row = cur.fetchone()
    if not row:
        return {"new_id": "", "original_id": id, "status": "error", "message": "Memory not found."}

    files = json.loads(row["files"]) if row["files"] else []
    tags = json.loads(row["tags"]) if row["tags"] else []

    result = await memory_propose(
        type=row["type"],
        content=row["content"],
        project=row["project"],
        files=files if files else None,
        tags=tags if tags else None,
        confidence=row["confidence"],
        source=row["source"],
        git_commit_hash=row["git_commit_hash"],
        git_branch=row["git_branch"],
    )

    if "id" in result and result["id"]:
        conn.execute("UPDATE memories SET status = ? WHERE id = ?", (MemoryStatus.REJECTED.value, id))
        conn.commit()
        return {
            "new_id": result["id"],
            "original_id": id,
            "status": "proposed",
        }
    return result


@mcp.tool
async def memory_get_knowledge_graph() -> dict:
    """Return the knowledge graph of memory relationships."""
    return knowledge_graph.to_dict()


@mcp.tool
async def memory_warm_cache(open_files: list[str], branch: str | None = None) -> dict:
    """Warm the L1 cache with memories relevant to the given open files."""
    count = fetch_cache.warm(open_files, branch)
    return {"warmed": count, "message": "L1 cache warmed with {} entries".format(count)}


@mcp.tool
async def memory_session_summary() -> dict:
    """
    Return counts of proposed and stale entries. Called on session startup.
    """
    cur = conn.execute(
        "SELECT COUNT(*) as n FROM memories WHERE status = ?",
        (MemoryStatus.PROPOSED.value,),
    )
    proposed_count = cur.fetchone()["n"]

    cur = conn.execute("SELECT COUNT(*) as n FROM memories WHERE stale = 1")
    stale_count = cur.fetchone()["n"]

    msg_parts = []
    if proposed_count > 0:
        msg_parts.append(f"{proposed_count} proposed")
    if stale_count > 0:
        msg_parts.append(f"{stale_count} stale")
    message = "; ".join(msg_parts) if msg_parts else "No pending actions"

    return {
        "proposed_count": proposed_count,
        "stale_count": stale_count,
        "message": message,
    }


if __name__ == "__main__":
    mcp.run()
