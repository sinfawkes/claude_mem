"""
Vector embedding store for semantic memory search.

Embeddings are generated with sentence-transformers (all-MiniLM-L6-v2, 384 dims)
and stored as BLOBs in a dedicated SQLite table. Similarity is computed in NumPy
over the L2 pre-filtered candidate set, which is small enough (typically 20-200
entries) that in-memory cosine comparison is fast without needing an ANN index.

The model is loaded lazily on first use to keep server startup fast.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import struct
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384
_model = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    memory_id TEXT PRIMARY KEY,
    embedding BLOB NOT NULL
);
"""


def _load_model(model_name: str = "all-MiniLM-L6-v2"):
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model '%s'...", model_name)
        _model = SentenceTransformer(model_name)
        logger.info("Embedding model loaded.")
    return _model


def _serialize(vector: list[float] | np.ndarray) -> bytes:
    arr = np.array(vector, dtype=np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _deserialize(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _get_conn(db_path: str) -> sqlite3.Connection:
    Path(os.path.expanduser(db_path)).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(os.path.expanduser(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    """Create the embeddings table if it does not exist."""
    conn = _get_conn(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def encode(content: str, model_name: str = "all-MiniLM-L6-v2") -> np.ndarray:
    """Return a normalised 384-dim embedding for the given text."""
    model = _load_model(model_name)
    vec = model.encode(content, normalize_embeddings=True)
    return vec.astype(np.float32)


def add_embedding(
    db_path: str,
    memory_id: str,
    content: str,
    model_name: str = "all-MiniLM-L6-v2",
) -> None:
    """Generate and store an embedding for a confirmed memory."""
    vector = encode(content, model_name)
    conn = _get_conn(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (memory_id, embedding) VALUES (?, ?)",
        (memory_id, _serialize(vector)),
    )
    conn.commit()
    conn.close()
    logger.debug("Stored embedding for memory %s", memory_id)


def remove_embedding(db_path: str, memory_id: str) -> None:
    """Delete the embedding for a memory (on delete or revert-to-proposed)."""
    conn = _get_conn(db_path)
    conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (memory_id,))
    conn.commit()
    conn.close()


def vector_search(
    db_path: str,
    query: str,
    candidate_ids: Optional[list[str]] = None,
    top_k: int = 10,
    model_name: str = "all-MiniLM-L6-v2",
) -> list[tuple[str, float]]:
    """
    Return the top-k (memory_id, similarity_score) pairs for the query.

    If candidate_ids is provided, only those IDs are considered (L2 pre-filter).
    Similarity scores are in [0, 1]; higher is more similar.
    """
    query_vec = encode(query, model_name)

    conn = _get_conn(db_path)
    if candidate_ids is not None:
        if not candidate_ids:
            conn.close()
            return []
        placeholders = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"SELECT memory_id, embedding FROM embeddings WHERE memory_id IN ({placeholders})",
            candidate_ids,
        ).fetchall()
    else:
        rows = conn.execute("SELECT memory_id, embedding FROM embeddings").fetchall()
    conn.close()

    if not rows:
        return []

    scored: list[tuple[str, float]] = []
    for row in rows:
        stored_vec = _deserialize(row["embedding"])
        score = _cosine_similarity(query_vec, stored_vec)
        scored.append((row["memory_id"], score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def has_embedding(db_path: str, memory_id: str) -> bool:
    """Return True if an embedding exists for the given memory_id."""
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT 1 FROM embeddings WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    conn.close()
    return row is not None


def count_embeddings(db_path: str) -> int:
    conn = _get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    conn.close()
    return n
