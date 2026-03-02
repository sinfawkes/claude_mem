#!/usr/bin/env python3
"""CLI tool for managing Claude Code persistent memory."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from config import MemoryType, MemoryStatus, MemorySource
from staleness import check_all_memories_staleness, is_git_repo
from memory_indexer import MemoryIndexer

load_dotenv()

app = typer.Typer(help="Claude Code persistent memory manager")
console = Console()

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


def _get_db_path() -> str:
    path = os.environ.get("MEMORY_DB_PATH", "~/.claude-memory/data/memory.db")
    return os.path.expanduser(path)


def _get_git_root() -> str:
    root = os.environ.get("GIT_ROOT", os.getcwd())
    return os.path.expanduser(root)


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "files" in d and isinstance(d.get("files"), str):
        d["files"] = json.loads(d["files"]) if d["files"] else []
    if "tags" in d and isinstance(d.get("tags"), str):
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    if "stale" in d and isinstance(d.get("stale"), int):
        d["stale"] = bool(d["stale"])
    return d


def _capture_git_state(cwd: str) -> tuple[Optional[str], Optional[str]]:
    try:
        hash_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit = hash_result.stdout.strip() if hash_result.returncode == 0 else None
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
        return (commit, branch)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return (None, None)


def _status_color(status: str, stale: bool = False) -> str:
    if stale:
        return "[yellow]stale[/yellow]"
    if status == MemoryStatus.PROPOSED.value:
        return "[yellow]proposed[/yellow]"
    if status == MemoryStatus.CONFIRMED.value:
        return "[green]confirmed[/green]"
    if status == MemoryStatus.REJECTED.value:
        return "[red]rejected[/red]"
    return status


@app.command()
def memory_add(
    content: str = typer.Argument(..., help="Memory content to add"),
    type: MemoryType = typer.Option(
        MemoryType.CONVENTION,
        "--type",
        "-t",
        help="Memory type: code_pattern, architecture_decision, convention, bug_fix, feature, documentation",
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project name"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags"),
    files: Optional[str] = typer.Option(None, "--files", help="Comma-separated file paths"),
) -> None:
    """Add and auto-confirm a memory (manual source)."""
    conn = _get_conn()
    git_root = _get_git_root()
    commit, branch = _capture_git_state(git_root)
    memory_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    files_list = [f.strip() for f in files.split(",")] if files else []

    conn.execute(
        """
        INSERT INTO memories (
            id, type, content, status, version, project, files, tags,
            confidence, source, git_commit_hash, git_branch, stale,
            last_accessed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            memory_id,
            type.value,
            content.strip(),
            MemoryStatus.CONFIRMED.value,
            project,
            json.dumps(files_list),
            json.dumps(tags_list),
            0.9,
            MemorySource.MANUAL.value,
            commit,
            branch,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    console.print(f"Added and confirmed memory [green]{memory_id}[/green] (type: {type.value})")


@app.command()
def memory_list(
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Filter by status: proposed, confirmed, rejected",
    ),
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
) -> None:
    """List memories with optional status and project filters."""
    conn = _get_conn()
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if project:
        clauses.append("project = ?")
        params.append(project)
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = conn.execute(
        f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC",
        params,
    ).fetchall()
    conn.close()

    table = Table(title="Memories")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Type")
    table.add_column("Content", max_width=50)
    table.add_column("Status")
    table.add_column("Project")
    for row in rows:
        d = _row_to_dict(row)
        status_str = d.get("status", "")
        stale = d.get("stale", False)
        table.add_row(
            d["id"][:8] + "...",
            d.get("type", ""),
            (d.get("content", "") or "")[:47] + "..."
            if len(d.get("content", "") or "") > 50
            else (d.get("content", "") or ""),
            _status_color(status_str, stale),
            d.get("project") or "-",
        )
    console.print(table)


def _resolve_id(conn: sqlite3.Connection, id_prefix: str) -> Optional[str]:
    """Resolve a full or partial memory ID. Returns the full ID or None if not found/ambiguous."""
    if len(id_prefix) == 36:
        return id_prefix
    rows = conn.execute(
        "SELECT id FROM memories WHERE id LIKE ?", (id_prefix + "%",)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        console.print(f"[red]Ambiguous prefix '{id_prefix}' matches {len(rows)} entries. Use more characters.[/red]")
    return None


@app.command()
def memory_confirm(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix) to confirm"),
) -> None:
    """Confirm a proposed memory."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    id = full_id
    row = conn.execute(
        "SELECT id, content, version, files FROM memories WHERE id = ? AND status = ?",
        (id, MemoryStatus.PROPOSED.value),
    ).fetchone()
    if not row:
        console.print("[red]Memory not found or already confirmed/rejected.[/red]")
        conn.close()
        raise typer.Exit(1)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE memories SET status = ?, last_accessed = ?, updated_at = ? WHERE id = ?",
        (MemoryStatus.CONFIRMED.value, now, now, id),
    )
    conn.execute(
        "INSERT INTO memory_versions (id, version, content, updated_at, git_commit_hash) VALUES (?, ?, ?, ?, NULL)",
        (id, row["version"], row["content"], now),
    )
    conn.commit()
    conn.close()
    console.print(f"Confirmed memory [green]{id}[/green]")


@app.command()
def memory_reject(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix) to reject"),
) -> None:
    """Reject a proposed memory."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    id = full_id
    conn.execute("UPDATE memories SET status = ? WHERE id = ?", (MemoryStatus.REJECTED.value, id))
    conn.commit()
    conn.close()
    console.print(f"Rejected memory [red]{id}[/red]")


@app.command()
def memory_search(
    query: str = typer.Argument(..., help="Search query"),
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
    type_filter: Optional[MemoryType] = typer.Option(
        None,
        "--type",
        help="Filter by memory type",
    ),
    top_k: int = typer.Option(10, "--top-k", help="Max results to return"),
) -> None:
    """Search confirmed memories."""
    from fetch_cache import MemoryFetchCache

    db_path = _get_db_path()
    fetch_cache = MemoryFetchCache(db_path)
    type_list = [type_filter.value] if type_filter else None
    rows = fetch_cache.search(
        query=query,
        project=project,
        type_filter=type_list,
        tags=None,
        top_k=top_k,
        bypass_prefilter=False,
    )
    table = Table(title="Search Results")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Type")
    table.add_column("Content", max_width=60)
    for r in rows:
        content = r.get("content", "") or ""
        table.add_row(
            r["id"][:8] + "...",
            r.get("type", ""),
            content[:57] + "..." if len(content) > 60 else content,
        )
    console.print(table)


@app.command()
def memory_get(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix)"),
) -> None:
    """Show full detail of one memory."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (full_id,)).fetchone()
    conn.close()
    if not row:
        console.print("[red]Memory not found.[/red]")
        raise typer.Exit(1)
    d = _row_to_dict(row)
    for k, v in d.items():
        console.print(f"[dim]{k}:[/dim] {v}")


@app.command()
def memory_update(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix)"),
    new_content: str = typer.Argument(..., help="New content"),
) -> None:
    """Update a memory (re-proposes it)."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    id = full_id
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
    if not row:
        console.print("[red]Memory not found.[/red]")
        conn.close()
        raise typer.Exit(1)

    old_version = row["version"]
    old_content = row["content"]
    old_status = row["status"]
    now = datetime.now(timezone.utc).isoformat()
    new_version = old_version + 1

    conn.execute(
        "INSERT INTO memory_versions (id, version, content, updated_at, git_commit_hash) VALUES (?, ?, ?, ?, NULL)",
        (id, old_version, old_content, now),
    )
    new_status = MemoryStatus.PROPOSED.value if old_status == MemoryStatus.CONFIRMED.value else old_status
    conn.execute(
        "UPDATE memories SET content = ?, version = ?, updated_at = ?, status = ? WHERE id = ?",
        (new_content.strip(), new_version, now, new_status, id),
    )
    conn.commit()
    conn.close()
    console.print(f"Updated memory [yellow]{id}[/yellow] (status: {new_status})")


@app.command()
def memory_delete(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix) to delete"),
    force: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a memory (with confirmation prompt)."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    id = full_id

    if not force:
        confirm = typer.confirm("Delete this memory?")
        if not confirm:
            conn.close()
            raise typer.Exit(0)

    conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    console.print(f"Deleted memory [red]{id}[/red]")


@app.command()
def memory_check_staleness(
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project"),
) -> None:
    """List stale confirmed memories."""
    conn = _get_conn()
    clauses = ["status = ?"]
    params: list = [MemoryStatus.CONFIRMED.value]
    if project:
        clauses.append("project = ?")
        params.append(project)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT id, type, content, project, files, tags, git_commit_hash, git_branch FROM memories WHERE {where}",
        params,
    ).fetchall()
    conn.close()

    memories = [_row_to_dict(row) for row in rows]
    git_root = _get_git_root()
    results = check_all_memories_staleness(memories, git_root)
    stale_results = [r for r in results if r.get("stale")]

    if not stale_results:
        console.print("[green]No stale memories.[/green]")
        return

    table = Table(title="Stale Memories")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Type")
    table.add_column("Preview", max_width=40)
    table.add_column("Changed Files")
    for r in stale_results:
        changed = ", ".join(r.get("changed_files", [])[:3])
        if len(r.get("changed_files", [])) > 3:
            changed += "..."
        table.add_row(
            r["memory_id"][:8] + "...",
            r.get("memory_type", ""),
            (r.get("memory_content_preview", "") or "")[:37] + "..."
            if len(r.get("memory_content_preview", "") or "") > 40
            else (r.get("memory_content_preview", "") or ""),
            changed or "-",
        )
    console.print(table)


@app.command()
def memory_refresh(
    id: str = typer.Argument(..., help="Memory ID (or unique prefix) to refresh"),
) -> None:
    """Re-propose a memory with current git state."""
    conn = _get_conn()
    full_id = _resolve_id(conn, id)
    if not full_id:
        console.print(f"[red]No memory found matching '{id}'.[/red]")
        conn.close()
        raise typer.Exit(1)
    id = full_id
    row = conn.execute(
        "SELECT id, type, content, project, files, tags, confidence, source FROM memories WHERE id = ?",
        (id,),
    ).fetchone()
    if not row:
        console.print("[red]Memory not found.[/red]")
        conn.close()
        raise typer.Exit(1)

    commit, branch = _capture_git_state(_get_git_root())
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    files_json = row["files"] or "[]"
    tags_json = row["tags"] or "[]"

    conn.execute(
        """
        INSERT INTO memories (
            id, type, content, status, version, project, files, tags,
            confidence, source, git_commit_hash, git_branch, stale,
            last_accessed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
        """,
        (
            new_id,
            row["type"],
            row["content"],
            MemoryStatus.PROPOSED.value,
            row["project"],
            files_json,
            tags_json,
            row["confidence"],
            row["source"],
            commit,
            branch,
            now,
            now,
        ),
    )
    conn.execute("UPDATE memories SET status = ? WHERE id = ?", (MemoryStatus.REJECTED.value, id))
    conn.commit()
    conn.close()
    console.print(f"Refreshed: new [yellow]{new_id}[/yellow] (proposed), original {id} rejected")


@app.command()
def memory_export(
    output_file: str = typer.Argument(..., help="Output JSON file path"),
) -> None:
    """Export confirmed memories as JSON."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE status = ?",
        (MemoryStatus.CONFIRMED.value,),
    ).fetchall()
    conn.close()

    data = [_row_to_dict(row) for row in rows]
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    console.print(f"Exported [green]{len(data)}[/green] memories to {output_file}")


@app.command()
def memory_import(
    input_file: str = typer.Argument(..., help="Input JSON file path"),
) -> None:
    """Import memories from JSON (as proposed, requiring confirmation)."""
    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    for entry in data:
        memory_id = str(uuid.uuid4())
        mem_type = entry.get("type", MemoryType.CONVENTION.value)
        content = entry.get("content", "")
        if not content:
            continue
        project = entry.get("project")
        files = entry.get("files", [])
        tags = entry.get("tags", [])
        files_json = json.dumps(files) if isinstance(files, list) else (files if isinstance(files, str) else "[]")
        tags_json = json.dumps(tags) if isinstance(tags, list) else (tags if isinstance(tags, str) else "[]")

        conn.execute(
            """
            INSERT INTO memories (
                id, type, content, status, version, project, files, tags,
                confidence, source, git_commit_hash, git_branch, stale,
                last_accessed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?, ?)
            """,
            (
                memory_id,
                mem_type,
                content.strip(),
                MemoryStatus.PROPOSED.value,
                project,
                files_json,
                tags_json,
                entry.get("confidence", 0.9),
                MemorySource.PROPOSED.value,
                now,
                now,
            ),
        )
        imported += 1
    conn.commit()
    conn.close()
    console.print(f"Imported [yellow]{imported}[/yellow] memories (proposed)")


@app.command()
def memory_index(
    project_path: str = typer.Argument(..., help="Project directory to scan"),
    project_name: Optional[str] = typer.Option(None, "--project-name", "-n", help="Project name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only print proposals, do not insert"),
) -> None:
    """Scan project and propose memory entries."""
    path = os.path.expanduser(project_path)
    name = project_name or os.path.basename(path.rstrip("/"))
    indexer = MemoryIndexer(project_path=path, dry_run=dry_run)
    patterns = indexer.index_project(name)

    if dry_run:
        for p in patterns:
            console.print(f"[dim]Proposed:[/dim] {p.type} | {p.content[:60]}... | files={p.files}")
        console.print(f"[dim]Dry run: {len(patterns)} patterns (not inserted)[/dim]")
        return

    conn = _get_conn()
    commit = indexer.get_git_commit_hash()
    branch = indexer.get_git_branch()
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for p in patterns:
        memory_id = str(uuid.uuid4())
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
                p.type,
                p.content,
                MemoryStatus.PROPOSED.value,
                name,
                json.dumps(p.files),
                json.dumps(p.tags),
                p.confidence,
                MemorySource.AUTO_GENERATED.value,
                commit,
                branch,
                now,
                now,
            ),
        )
        inserted += 1
        console.print(f"Proposed [yellow]{memory_id[:8]}...[/yellow] {p.content[:50]}...")
    conn.commit()
    conn.close()
    console.print(f"Inserted [green]{inserted}[/green] proposed memories")


if __name__ == "__main__":
    app()
