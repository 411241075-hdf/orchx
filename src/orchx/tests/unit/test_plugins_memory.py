"""Тесты для memory-плагинов (P0.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchx.plugins.memory.noop import NoopMemory
from orchx.plugins.memory.sqlite import (
    SqliteMemory,
    _cosine,
    _pack_floats,
    _sanitize_fts_query,
    _unpack_floats,
)

# NB: pytestmark.asyncio убран чтобы sync-тесты в конце файла
# (pack/unpack/cosine/sanitize) не помечались as async. async-тесты
# помечены вручную через @pytest.mark.asyncio.


# ---- Noop ----


@pytest.mark.asyncio
async def test_noop_memory_does_nothing():
    m = NoopMemory()
    await m.remember("plans", "T1", {"x": 1})
    res = await m.recall("plans", "x")
    assert res == []
    assert await m.forget_old(7) == 0


# ---- SQLite ----


@pytest.fixture
def sqlite_mem(tmp_path: Path):
    return SqliteMemory(path=str(tmp_path / "mem.db"))


@pytest.mark.asyncio
async def test_sqlite_remember_and_recall_fts(sqlite_mem: SqliteMemory):
    await sqlite_mem.remember(
        "plans", "T1", {"goal": "build authentication module", "files": ["auth.py"]}
    )
    await sqlite_mem.remember(
        "plans", "T2", {"goal": "fix payment bug in checkout"}
    )
    res = await sqlite_mem.recall("plans", "authentication")
    assert len(res) == 1
    assert res[0]["key"] == "T1"
    assert "authentication" in res[0]["value"]["goal"]


@pytest.mark.asyncio
async def test_sqlite_recall_no_results_returns_empty(sqlite_mem: SqliteMemory):
    res = await sqlite_mem.recall("plans", "nonexistent_topic_xyz")
    assert res == []


@pytest.mark.asyncio
async def test_sqlite_namespace_isolation(sqlite_mem: SqliteMemory):
    await sqlite_mem.remember("plans", "T1", {"goal": "auth"})
    await sqlite_mem.remember("failures", "F1", {"reason": "auth broke"})
    plans = await sqlite_mem.recall("plans", "auth")
    failures = await sqlite_mem.recall("failures", "auth")
    assert len(plans) == 1 and plans[0]["key"] == "T1"
    assert len(failures) == 1 and failures[0]["key"] == "F1"


@pytest.mark.asyncio
async def test_sqlite_upsert_on_same_key(sqlite_mem: SqliteMemory):
    await sqlite_mem.remember("plans", "T1", {"goal": "first"})
    await sqlite_mem.remember("plans", "T1", {"goal": "second"})
    res = await sqlite_mem.recall("plans", "second")
    assert len(res) == 1
    assert res[0]["value"]["goal"] == "second"


@pytest.mark.asyncio
async def test_sqlite_forget_old(sqlite_mem: SqliteMemory):
    await sqlite_mem.remember("plans", "T1", {"goal": "ancient"})
    # Set created_at to 100 days ago via direct sqlite manipulation.
    import sqlite3
    import time

    with sqlite3.connect(str(sqlite_mem.path)) as conn:
        conn.execute(
            "UPDATE memories SET created_at = ?, last_used_at = NULL WHERE key = ?",
            (time.time() - 100 * 86400, "T1"),
        )
    deleted = await sqlite_mem.forget_old(days=90)
    assert deleted == 1
    res = await sqlite_mem.recall("plans", "ancient")
    assert res == []


@pytest.mark.asyncio
async def test_sqlite_k_limits_results(sqlite_mem: SqliteMemory):
    for i in range(10):
        await sqlite_mem.remember("plans", f"T{i}", {"goal": f"task {i} about auth"})
    res = await sqlite_mem.recall("plans", "auth", k=3)
    assert len(res) <= 3


@pytest.mark.asyncio
async def test_sqlite_recall_with_zero_k(sqlite_mem: SqliteMemory):
    await sqlite_mem.remember("plans", "T1", {"goal": "auth"})
    res = await sqlite_mem.recall("plans", "auth", k=0)
    assert res == []


# ---- helpers ----


def test_pack_unpack_floats_roundtrip():
    v = [1.0, -2.5, 0.0, 3.14159]
    packed = _pack_floats(v)
    unpacked = _unpack_floats(packed)
    assert len(unpacked) == len(v)
    for a, b in zip(v, unpacked, strict=False):
        assert abs(a - b) < 1e-5


def test_cosine_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-6


def test_cosine_orthogonal_is_zero():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(_cosine(a, b)) < 1e-6


def test_cosine_empty_or_mismatched_returns_zero():
    assert _cosine([], []) == 0.0
    assert _cosine([1.0], [1.0, 2.0]) == 0.0
    assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_sanitize_fts_query_strips_punctuation():
    assert _sanitize_fts_query("auth!@#$ system?") == "auth system"


def test_sanitize_fts_query_empty_returns_original():
    # Если после очистки осталась пустота — fallback на исходник.
    assert _sanitize_fts_query("x") == "x"
