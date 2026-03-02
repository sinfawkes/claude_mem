# Claude Code Persistent Memory System

## Overview

A persistent memory system for Claude Code that stores code patterns, conventions, architecture decisions, and other project knowledge. Memories are proposed by automated indexing or by the model during sessions, then confirmed by humans before being added to the search index. Memories are anchored to git commits for staleness detection when referenced files change.

Each project keeps its own `project_memory.db` file inside the project root, making memory self-contained and portable alongside the code.

## Architecture

- **Three-tier fetch**: L1 in-process LRU cache → L2 SQL pre-filter (project/type/status) → L3 text/vector search on candidates
- **Human-in-the-loop**: Proposed memories require `memory_confirm` before appearing in search; rejected entries are archived
- **Git-anchored versioning**: `git_commit_hash` and `files` enable staleness checks when code changes
- **Per-project DB**: `project_memory.db` lives inside the project root, gitignored, isolated from other projects

```
┌─────────────────────────────────────────────────────────────────┐
│                        memory_search                             │
└─────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
   ┌─────────┐               ┌─────────────┐             ┌─────────────┐
   │   L1    │  cache miss   │     L2      │  candidates │     L3      │
   │  LRU    │ ───────────▶  │ SQL filter  │ ──────────▶│ text/vector │
   │  cache  │               │ project/    │             │   search    │
   └─────────┘               │ type/tags   │             └─────────────┘
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
```

## Setting Up a Project

Run `memory-init` once per project. It creates `project_memory.db` inside the project root, writes `.claude.json` to register the MCP server, and adds the DB to `.gitignore`:

```bash
python memory_manager.py memory-init /path/to/your/project
```

Example output:

```
Project: scroll_1
DB:      /path/to/scroll_1/project_memory.db

Write .claude.json to /path/to/scroll_1/.claude.json? [Y/n]:
Add project_memory.db to .gitignore? [Y/n]:

Done. Restart Claude Code in /path/to/scroll_1 to activate memory.
```

After init, each project looks like this:

```
your_project/
  ├── project_memory.db     ← memory DB, gitignored
  ├── .claude.json          ← MCP server registration
  └── ...
```

Repeat for each project — every project gets its own isolated DB.

### Options

```bash
python memory_manager.py memory-init --help

# Custom project name
python memory_manager.py memory-init /path/to/project --name my_project

# Custom DB filename
python memory_manager.py memory-init /path/to/project --db-filename memory.db
```

### What .claude.json looks like after init

```json
{
  "mcpServers": {
    "memory": {
      "command": "/path/to/claude_mem/.venv/bin/python",
      "args": ["/path/to/claude_mem/server.py"],
      "env": {
        "MEMORY_DB_PATH": "/path/to/your/project/project_memory.db",
        "PROJECT_PATH": "/path/to/your/project",
        "GIT_ROOT": "/path/to/your/project"
      }
    }
  }
}
```

Claude Code picks this up automatically when you open the project — no global config needed.

## Environment Variables

All variables are set per-project inside `.claude.json` (via `memory-init`). You can also override them with a `.env` file in the `claude_mem` directory for local development:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_DB_PATH` | — | Path to the project's `project_memory.db` (set by `memory-init`) |
| `EMBEDDINGS_DB_PATH` | — | Path to vector embeddings DB (optional, for future vector search) |
| `PROJECT_PATH` | — | Project directory root |
| `GIT_ROOT` | — | Git repository root for staleness detection |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model for vector embeddings |
| `MAX_LRU_CACHE_SIZE` | `100` | Maximum entries in the L1 in-process cache per session |
| `PROPOSAL_RATE_LIMIT_PER_MINUTE` | `10` | Max auto-indexer proposals per minute (prevents flooding) |
| `PROPOSAL_TTL_DAYS` | `7` | Days before unreviewed proposed entries are auto-expired |

## Global Claude Code Instruction

Create `~/.claude/CLAUDE.md` to tell Claude to always use the MCP memory tools instead of writing `MEMORY.md` files:

```markdown
## Memory System

A persistent MCP memory server is configured. Always use it instead of writing MEMORY.md files.

- Never write to MEMORY.md files
- Use `memory_propose` when you learn something new, then ask the user to confirm
- Use `memory_search` before answering questions about the codebase
- Call `memory_session_summary` at the start of each session
```

## CLI Quick Reference

All commands accept a full UUID or a unique prefix (e.g. `b80ef0d8`) for ID arguments.

| Command | Key Options | Description |
|---------|-------------|-------------|
| `memory-init <path>` | `--name`, `--db-filename` | Bootstrap memory for a new project |
| `memory-add <content>` | `--type`, `--project`, `--tags`, `--files` | Add and auto-confirm a memory |
| `memory-list` | `--status`, `--project` | List memories with optional filters |
| `memory-confirm <id>` | | Confirm a proposed memory |
| `memory-reject <id>` | | Reject a proposed memory |
| `memory-search <query>` | `--project`, `--type`, `--top-k` | Search confirmed memories |
| `memory-get <id>` | | Show full detail of one memory |
| `memory-update <id> <content>` | | Update a memory (re-proposes it) |
| `memory-delete <id>` | `--yes` | Delete a memory |
| `memory-check-staleness` | `--project` | List stale confirmed memories |
| `memory-refresh <id>` | | Re-propose a memory with current git state |
| `memory-export <file>` | | Export confirmed memories as JSON |
| `memory-import <file>` | | Import memories from JSON (as proposed) |
| `memory-index <path>` | `--project-name`, `--dry-run` | Scan project and propose entries |

```bash
python memory_manager.py <command> --help   # detailed help for any command
```

## Memory Types

The server normalises type strings automatically — you don't need to use the exact value:

| Type | Also accepts |
|------|-------------|
| `code_pattern` | `code_*`, `pattern`, `implementation_pattern`, `design_pattern` |
| `architecture_decision` | `architecture_*`, `architectural_*`, `design_decision` |
| `convention` | `coding_convention`, `style`, `guideline`, `standard` |
| `bug_fix` | `bug`, `fix`, `bugfix`, `hotfix` |
| `feature` | `functionality`, `capability` |
| `documentation` | `doc`, `docs`, `note`, `guide` |

## Memory Lifecycle

```
memory_propose()  →  status=proposed  →  user confirms?
                                              │
                                    ┌─────────┴─────────┐
                                   Yes                  No
                                    │                    │
                             memory_confirm()    memory_reject()
                             status=confirmed    status=rejected
                             enters search       excluded forever
                                    │
                          code changes later?
                                    │
                          memory_check_staleness()
                                    │
                             stale entries flagged
                                    │
                             memory_refresh()
                             re-proposes with new git hash
```

1. **Propose**: `memory_propose` (MCP) or `memory-index` (CLI) creates entries with `status=proposed`
2. **Review**: `memory_session_summary` reports pending proposals and stale counts at session startup
3. **Confirm**: `memory_confirm` moves to `status=confirmed` and adds to the search index
4. **Reject**: `memory_reject` sets `status=rejected` — kept for audit, excluded from search
5. **Update**: editing a confirmed memory reverts it to `proposed`, requiring re-confirmation

## Staleness Detection

Every confirmed memory stores the `git_commit_hash` at the time it was captured. When you run staleness checks, the system runs `git log <hash>..HEAD -- <files>` for each memory's referenced files.

```bash
# Check stale memories from CLI
python memory_manager.py memory-check-staleness --project scroll_1

# Or ask Claude Code directly
# "Check if any of your memories about the battle system are stale."
```

Stale memories remain searchable but are flagged. Use `memory-refresh` to re-propose with the current git state.

## Running Tests

```bash
cd /path/to/claude_mem
source .venv/bin/activate
pytest tests/ -v --tb=short
```

## Project Structure

| File | Description |
|------|-------------|
| `server.py` | FastMCP server — all MCP tools, SQLite schema, type normalisation |
| `config.py` | Pydantic v2 models: `MemoryEntry`, `MemoryMetadata`, `MemorySearchQuery` |
| `fetch_cache.py` | L1 LRU cache + L2 SQL pre-filter + L3 text/vector search |
| `knowledge_graph.py` | NetworkX digraph tracking memory–file relationships |
| `memory_indexer.py` | Project file scanner — proposes entries, never auto-confirms |
| `staleness.py` | Git-based staleness detection via `git log` diff |
| `memory_manager.py` | Typer CLI for all memory operations |
| `tests/` | Unit tests for models, fetch tiers, and staleness logic |
