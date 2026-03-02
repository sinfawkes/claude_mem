"""Tests for vector_store.py — embedding generation and similarity search."""

from __future__ import annotations

import struct
import os
import pytest

import vector_store


@pytest.fixture
def vec_db(tmp_path):
    db = str(tmp_path / "test_embeddings.db")
    vector_store.init_db(db)
    return db


def test_init_db_creates_table(vec_db):
    import sqlite3
    conn = sqlite3.connect(vec_db)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_encode_returns_correct_dim():
    vec = vector_store.encode("battle command dispatch")
    assert vec.shape == (384,)


def test_encode_is_normalised():
    import numpy as np
    vec = vector_store.encode("some content")
    norm = float(np.linalg.norm(vec))
    assert abs(norm - 1.0) < 1e-5


def test_add_and_has_embedding(vec_db):
    vector_store.add_embedding(vec_db, "mem-1", "battle command pattern")
    assert vector_store.has_embedding(vec_db, "mem-1") is True


def test_has_embedding_false_for_missing(vec_db):
    assert vector_store.has_embedding(vec_db, "does-not-exist") is False


def test_remove_embedding(vec_db):
    vector_store.add_embedding(vec_db, "mem-2", "some content")
    assert vector_store.has_embedding(vec_db, "mem-2") is True
    vector_store.remove_embedding(vec_db, "mem-2")
    assert vector_store.has_embedding(vec_db, "mem-2") is False


def test_count_embeddings(vec_db):
    assert vector_store.count_embeddings(vec_db) == 0
    vector_store.add_embedding(vec_db, "m1", "content one")
    vector_store.add_embedding(vec_db, "m2", "content two")
    assert vector_store.count_embeddings(vec_db) == 2


def test_vector_search_returns_most_similar(vec_db):
    vector_store.add_embedding(vec_db, "battle", "battle command dispatch uses flat buffer")
    vector_store.add_embedding(vec_db, "render", "render engine manages draw call batching")
    vector_store.add_embedding(vec_db, "network", "network layer handles TCP packet framing")

    results = vector_store.vector_search(vec_db, "how are battle commands dispatched", top_k=3)
    assert len(results) > 0
    # The battle entry should rank first
    top_id, top_score = results[0]
    assert top_id == "battle"
    assert top_score > 0.3


def test_vector_search_with_candidate_filter(vec_db):
    vector_store.add_embedding(vec_db, "battle", "battle command dispatch")
    vector_store.add_embedding(vec_db, "render", "render engine draw calls")

    # Only allow "render" as a candidate — even though "battle" would rank higher semantically
    results = vector_store.vector_search(
        vec_db, "battle command", candidate_ids=["render"], top_k=5
    )
    assert len(results) == 1
    assert results[0][0] == "render"


def test_vector_search_empty_candidates_returns_empty(vec_db):
    vector_store.add_embedding(vec_db, "x", "some content")
    results = vector_store.vector_search(vec_db, "query", candidate_ids=[], top_k=5)
    assert results == []


def test_upsert_replaces_existing(vec_db):
    vector_store.add_embedding(vec_db, "mem-3", "original content")
    vector_store.add_embedding(vec_db, "mem-3", "updated content")
    assert vector_store.count_embeddings(vec_db) == 1
