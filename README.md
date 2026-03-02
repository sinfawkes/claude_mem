# Claude Code Persistent Memory System

## Overview

A persistent memory system for Claude Code that stores code patterns, conventions, architecture decisions, and other project knowledge. Memories are proposed by automated indexing or by the model during sessions, then confirmed by humans before being added to the search index. Memories can be anchored to git commits for staleness detection when referenced files change.

## Architecture

- **Three-tier fetch**: L1 in-process LRU cache → L2 SQL pre-filter (project/type/status) → L3 text/vector search on candidates
- **Human-in-the-loop**: Proposed memories require `memory_confirm` before appearing in search; rejected entries are archived
- **Git-anchored versioning**: `git_commit_hash` and `files` enable staleness checks when code changes

```
┌─────────────────────────────────────────────────────────────────┐
│                        memory_search                             │
└─────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
   ┌─────────┐               ┌─────────────┐             ┌─────────────┐
   │   L1    │  cache miss   │     L2     │  candidates │     L3      │
   │  LRU    │ ───────────▶  │ SQL filter │ ──────────▶│ text/vector │
   │  cache  │               │ project/   │             │   search    │
   └─────────┘               │ type/tags  │             └─────────────┘
        ▲                    └─────────────┘
        │                            │
        │                            ▼
        │                     ┌─────────────┐
        └─────────────────────│   SQLite    │
              cache hit       │  memories   │
                              └─────────────┘
```

## Installation

```bash
cd /path/to/claude_mem
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your paths and settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | `~/.claude-memory/data/memory.db` | SQLite database for memories |
| `EMBEDDINGS_DB_PATH` | `~/.claude-memory/data/embeddings.db` | SQLite database for embeddings (vector search) |
| `PROJECT_PATH` | — | Project directory to index |
| `GIT_ROOT` | — | Git repository root for staleness detection |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model for vector embeddings |
| `MAX_LRU_CACHE_SIZE` | `100` | Maximum number of entries held in the L1 in-process cache per session |
| `PROPOSAL_RATE_LIMIT_PER_MINUTE` | `10` | Maximum auto-indexer proposals per minute (prevents flooding during large scans) |
| `PROPOSAL_TTL_DAYS` | `7` | Days before unreviewed proposed entries are auto-expired |

## Claude Code MCP Configuration

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "uvx",
      "args": ["run", "fastmcp", "dev", "/path/to/claude_mem/server.py"]
    }
  }
}
```

Or with a virtualenv:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/path/to/claude_mem/.venv/bin/python",
      "args": ["/path/to/claude_mem/server.py"]
    }
  }
}
```

## CLI Quick Reference

| Command | Description |
|---------|-------------|
| `memory_add` | Add and auto-confirm a memory (manual source) |
| `memory_list` | List memories with optional status and project filters |
| `memory_confirm` | Confirm a proposed memory |
| `memory_reject` | Reject a proposed memory |
| `memory_search` | Search confirmed memories |
| `memory_get` | Show full detail of one memory |
| `memory_update` | Update a memory (re-proposes it) |
| `memory_delete` | Delete a memory (with confirmation) |
| `memory_check_staleness` | List stale confirmed memories |
| `memory_refresh` | Re-propose a memory with current git state |
| `memory_export` | Export confirmed memories as JSON |
| `memory_import` | Import memories from JSON (as proposed) |
| `memory_index` | Scan project and propose memory entries |

Run with `python -m memory_manager <command> --help` for options.

## Memory Lifecycle

1. **Propose**: `memory_propose` (MCP) or `memory_index` (CLI) creates entries with `status=proposed`
2. **Review**: `memory_session_summary` (MCP) reports proposed and stale counts at session startup
3. **Confirm**: `memory_confirm` moves to `status=confirmed` and adds to search index
4. **Reject**: `memory_reject` sets `status=rejected`

Updated confirmed memories revert to proposed and require re-confirmation.

## Staleness Detection

Memories with `git_commit_hash` and `files` are checked against git history. If any referenced file has commits since the capture, the memory is marked stale.

- Run `memory_check_staleness` (CLI) or `memory-check-staleness` (MCP) to list stale memories
- Use `memory_refresh` to re-propose a stale memory with current git metadata

## Running Tests

```bash
pytest tests/ -v --tb=short
```

## Project Structure

| File | Description |
|------|-------------|
| `server.py` | FastMCP server exposing memory tools |
| `config.py` | Pydantic models and enums |
| `fetch_cache.py` | L1/L2/L3 tiered fetch cache |
| `knowledge_graph.py` | Memory–file relationship graph |
| `memory_indexer.py` | Project scanner that proposes memories |
| `staleness.py` | Git-based staleness detection |
| `memory_manager.py` | CLI (Typer) for memory operations |
