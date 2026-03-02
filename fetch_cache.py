"""
Tiered fetch architecture for memory retrieval.

Tier 1: In-process LRU cache (hot memories per session)
Tier 2: SQL pre-filter (project/type/tags/status) — narrows candidate set
Tier 3: Vector similarity search on the filtered candidate IDs (falls back to
        LIKE text search when no embeddings DB is configured)
"""

from __future__ import annotations

import json
import sqlite3
import logging
from typing import Optional

from cachetools import LRUCache

logger = logging.getLogger(__name__)


class MemoryFetchCache:
    """Three-tier memory fetch: L1 LRU → L2 SQL pre-filter → L3 vector search."""

    def __init__(
        self,
        db_path: str,
        max_lru_size: int = 100,
        embeddings_db_path: Optional[str] = None,
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        self._db_path = db_path
        self._embeddings_db_path = embeddings_db_path
        self._embedding_model = embedding_model
        self._l1: LRUCache = LRUCache(maxsize=max_lru_size)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, memory_id: str) -> Optional[dict]:
        """Fetch a single memory by ID, checking L1 cache first."""
        if memory_id in self._l1:
            logger.debug("L1 cache hit for %s", memory_id)
            return self._l1[memory_id]

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND status = 'confirmed'",
                (memory_id,),
            ).fetchone()

        if row is None:
            return None

        result = dict(row)
        self._l1[memory_id] = result
        return result

    def prefilter(
        self,
        project: Optional[str] = None,
        type_filter: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> list[str]:
        """
        L2: Return confirmed memory IDs matching the given filters.
        Always restricts to status='confirmed'.
        """
        clauses = ["status = 'confirmed'"]
        params: list = []

        if project:
            clauses.append("project = ?")
            params.append(project)

        if type_filter:
            placeholders = ",".join("?" * len(type_filter))
            clauses.append("type IN ({})".format(placeholders))
            params.extend(type_filter)

        where = " AND ".join(clauses)
        sql = "SELECT id, tags FROM memories WHERE {}".format(where)

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        if not tags:
            return [r["id"] for r in rows]

        tag_set = set(tags)
        matched = []
        for row in rows:
            row_tags = set(json.loads(row["tags"] or "[]"))
            if tag_set & row_tags:
                matched.append(row["id"])
        return matched

    def _vector_search(
        self,
        query: str,
        candidate_ids: list[str],
        top_k: int,
    ) -> list[dict]:
        """L3: semantic vector search over the candidate set."""
        import vector_store

        ranked = vector_store.vector_search(
            db_path=self._embeddings_db_path,
            query=query,
            candidate_ids=candidate_ids,
            top_k=top_k,
            model_name=self._embedding_model,
        )
        if not ranked:
            return []

        ranked_ids = [mid for mid, _ in ranked]
        scores = {mid: score for mid, score in ranked}

        placeholders = ",".join("?" * len(ranked_ids))
        with self._get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders})",
                ranked_ids,
            ).fetchall()

        by_id = {row["id"]: dict(row) for row in rows}
        results = []
        for mid in ranked_ids:
            if mid in by_id:
                entry = by_id[mid]
                entry["similarity_score"] = round(scores[mid], 4)
                self._l1[mid] = entry
                results.append(entry)
        return results

    def _text_search(
        self,
        query: str,
        candidate_ids: list[str],
        top_k: int,
    ) -> list[dict]:
        """L3 fallback: LIKE-based search when no embeddings DB is configured."""
        if not candidate_ids:
            return []

        placeholders = ",".join("?" * len(candidate_ids))
        sql = """
            SELECT * FROM memories
            WHERE id IN ({})
              AND content LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
        """.format(placeholders)
        params = [*candidate_ids, "%" + query + "%", top_k]

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = [dict(r) for r in rows]
        for r in results:
            self._l1[r["id"]] = r
        return results

    def search(
        self,
        query: str,
        project: Optional[str] = None,
        type_filter: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        top_k: int = 10,
        bypass_prefilter: bool = False,
    ) -> list[dict]:
        """Full tiered search: L2 pre-filter → L3 vector (or text fallback)."""
        if bypass_prefilter:
            candidate_ids = self._all_confirmed_ids()
        else:
            candidate_ids = self.prefilter(project, type_filter, tags)
            logger.debug(
                "L2 pre-filter returned %d candidates (project=%s, types=%s, tags=%s)",
                len(candidate_ids), project, type_filter, tags,
            )

        if self._embeddings_db_path:
            logger.debug("L3: vector search over %d candidates", len(candidate_ids))
            return self._vector_search(query, candidate_ids, top_k)

        logger.debug("L3: text fallback search (no embeddings DB configured)")
        return self._text_search(query, candidate_ids, top_k)

    def _all_confirmed_ids(self) -> list[str]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM memories WHERE status = 'confirmed'"
            ).fetchall()
        return [r["id"] for r in rows]

    def warm(self, open_files: list[str], branch: Optional[str] = None) -> int:
        """
        Warm the L1 cache with confirmed memories relevant to the given open files.
        Returns the number of entries loaded into cache.
        """
        if not open_files:
            return 0

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE status = 'confirmed' ORDER BY last_accessed DESC LIMIT ?",
                (self._l1.maxsize,),
            ).fetchall()

        open_set = set(open_files)
        prioritised = []
        rest = []
        for row in rows:
            row_files = set(json.loads(row["files"] or "[]"))
            if open_set & row_files:
                prioritised.append(dict(row))
            else:
                rest.append(dict(row))

        for entry in prioritised + rest:
            if len(self._l1) >= self._l1.maxsize:
                break
            self._l1[entry["id"]] = entry

        logger.info("L1 cache warmed with %d entries", len(self._l1))
        return len(self._l1)

    def invalidate(self, memory_id: str) -> None:
        """Invalidate a single entry from the L1 cache."""
        self._l1.pop(memory_id, None)

    def clear(self) -> None:
        """Clear the entire L1 cache."""
        self._l1.clear()
