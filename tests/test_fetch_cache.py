import json
import sqlite3

from fetch_cache import MemoryFetchCache


def _insert_memory(conn, mem_id, project, mem_type, status, files=None):
    now = "2025-01-15T10:00:00+00:00"
    files_json = json.dumps(files or [])
    conn.execute(
        """INSERT INTO memories (id, type, content, status, version, project, files, tags,
           confidence, source, git_commit_hash, git_branch, stale, last_accessed, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?, '[]', 0.9, 'proposed', NULL, NULL, 0, NULL, ?, ?)""",
        (mem_id, mem_type, "content for " + mem_id, status, project, files_json, now, now),
    )


def test_l1_cache_miss_hits_db(tmp_db):
    conn = sqlite3.connect(tmp_db)
    mem_id = "cache-miss-1"
    _insert_memory(conn, mem_id, "proj", "code_pattern", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    cache.clear()
    row = cache.get(mem_id)
    assert row is not None
    assert row["id"] == mem_id
    assert row["status"] == "confirmed"


def test_l1_cache_hit(tmp_db):
    conn = sqlite3.connect(tmp_db)
    mem_id = "cache-hit-1"
    _insert_memory(conn, mem_id, "proj", "code_pattern", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    cache.clear()
    result1 = cache.get(mem_id)
    result2 = cache.get(mem_id)
    assert result1 is result2


def test_prefilter_by_project(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "pref-a1", "A", "code_pattern", "confirmed")
    _insert_memory(conn, "pref-a2", "A", "convention", "confirmed")
    _insert_memory(conn, "pref-b1", "B", "code_pattern", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    ids = cache.prefilter(project="A")
    assert len(ids) == 2
    assert set(ids) == {"pref-a1", "pref-a2"}


def test_prefilter_by_type(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "type-cp1", "p", "code_pattern", "confirmed")
    _insert_memory(conn, "type-cp2", "p", "code_pattern", "confirmed")
    _insert_memory(conn, "type-cv1", "p", "convention", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    ids = cache.prefilter(project="p", type_filter=["code_pattern"])
    assert len(ids) == 2
    assert set(ids) == {"type-cp1", "type-cp2"}


def test_prefilter_excludes_proposed(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "prop-1", "p", "code_pattern", "proposed")
    _insert_memory(conn, "conf-1", "p", "code_pattern", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    ids = cache.prefilter(project="p")
    assert ids == ["conf-1"]


def test_search_returns_confirmed_only(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "search-prop", "p", "code_pattern", "proposed", files=[])
    _insert_memory(conn, "search-conf", "p", "code_pattern", "confirmed", files=[])
    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("unique banana pineapple", "search-conf"),
    )
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    results = cache.search(query="banana", project="p")
    assert len(results) == 1
    assert results[0]["id"] == "search-conf"


def test_search_bypass_prefilter(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "bypass-1", "project-A", "code_pattern", "confirmed", files=[])
    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        ("special zebra keyword", "bypass-1"),
    )
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    results = cache.search(
        query="zebra",
        project="project-B",
        bypass_prefilter=True,
    )
    assert len(results) == 1
    assert results[0]["id"] == "bypass-1"


def test_warm_cache_prioritises_open_files(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _insert_memory(conn, "warm-1", "p", "code_pattern", "confirmed", files=["other.py"])
    _insert_memory(conn, "warm-2", "p", "code_pattern", "confirmed", files=["battle.cpp"])
    _insert_memory(conn, "warm-3", "p", "code_pattern", "confirmed", files=["main.py"])
    _insert_memory(conn, "warm-4", "p", "code_pattern", "confirmed", files=["battle.cpp"])
    _insert_memory(conn, "warm-5", "p", "code_pattern", "confirmed", files=["util.py"])
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    cache.clear()
    cache.warm(["battle.cpp"])
    assert "warm-2" in cache._l1
    assert "warm-4" in cache._l1


def test_invalidate_removes_from_l1(tmp_db):
    conn = sqlite3.connect(tmp_db)
    mem_id = "inv-1"
    _insert_memory(conn, mem_id, "p", "code_pattern", "confirmed")
    conn.commit()
    conn.close()

    cache = MemoryFetchCache(tmp_db)
    cache.clear()
    cache.get(mem_id)
    assert mem_id in cache._l1
    cache.invalidate(mem_id)
    assert mem_id not in cache._l1
