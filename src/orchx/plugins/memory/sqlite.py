"""SQLite + FTS5 memory backend (P0.3).

Storage:

* SQLite database (default: ``.orchx/memory.db``).
* Таблица ``memories``: ``id, namespace, repo_root, key, value (JSON),
  embedding (BLOB, optional), created_at, last_used_at, usage_count``.
* Virtual table ``memories_fts``: FTS5 индекс по полю ``value`` для
  text-search.

Embedding-search (опционально):

* Если задан ``embed_endpoint`` (OpenAI-compatible ``/v1/embeddings``),
  то новые memories сохраняют embedding, а ``recall(query)`` сначала
  пытается векторный поиск через cosine similarity, fallback на FTS5.
* Embedding-функция вызывается лениво (только если есть URL/ключ).

Namespaces (соглашение):

* ``plans``     — успешные планы прошлых прогонов.
* ``failures``  — провалы фаз (для replanner-контекста).
* ``fixes``     — успешные debug fixes (для debugger-контекста).
* ``reviews``   — review findings (для reviewer self-improvement).
* любой custom — клиент свободен.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SqliteMemory:
    """Persistent memory backend на SQLite + FTS5 + optional embeddings.

    Config:
        path: путь к SQLite файлу (default: ``.orchx/memory.db``).
        embed_endpoint: URL OpenAI-compatible /v1/embeddings (опционально).
        embed_model: имя embedding-модели (default: ``text-embedding-3-small``).
        embed_api_key: API-key для embed_endpoint (опционально).

    Thread-safety: SQLite connection пересоздаётся per-call (avoiding
    cross-thread issues; cost минимальный, файл локальный).
    """

    name = "sqlite"

    def __init__(
        self,
        *,
        path: str = ".orchx/memory.db",
        embed_endpoint: str | None = None,
        embed_model: str = "text-embedding-3-small",
        embed_api_key: str | None = None,
        **_: Any,
    ) -> None:
        self.path = Path(path)
        # Подстановка env-переменных, если path начинается с ~ или содержит $VAR.
        self.path = Path(os.path.expandvars(str(self.path))).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embed_endpoint = embed_endpoint
        self.embed_model = embed_model
        self.embed_api_key = embed_api_key or os.environ.get("OPENAI_API_KEY")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        # Включаем foreign keys и WAL для concurrent read.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    repo_root TEXT NOT NULL DEFAULT '',
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    embedding BLOB,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(namespace, repo_root, key)
                );

                CREATE INDEX IF NOT EXISTS idx_memories_ns_repo
                    ON memories(namespace, repo_root);

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    value,
                    content='memories',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai
                    AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, value) VALUES (new.id, new.value);
                    END;
                CREATE TRIGGER IF NOT EXISTS memories_ad
                    AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, value)
                            VALUES('delete', old.id, old.value);
                    END;
                CREATE TRIGGER IF NOT EXISTS memories_au
                    AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, value)
                            VALUES('delete', old.id, old.value);
                        INSERT INTO memories_fts(rowid, value)
                            VALUES (new.id, new.value);
                    END;
                """
            )

    # -----------------------------------------------------------------
    # Public Protocol methods
    # -----------------------------------------------------------------

    async def remember(
        self,
        namespace: str,
        key: str,
        value: dict[str, Any],
    ) -> None:
        repo_root = str(value.get("__repo_root__", ""))
        clean_value = {k: v for k, v in value.items() if not k.startswith("__")}
        value_json = json.dumps(clean_value, ensure_ascii=False, default=str)
        embedding_blob = None
        if self.embed_endpoint:
            text_for_embed = self._extract_embedding_text(clean_value)
            try:
                embedding = await self._embed(text_for_embed)
                if embedding:
                    embedding_blob = _pack_floats(embedding)
            except Exception as e:  # noqa: BLE001
                logger.debug("embedding failed: %s; storing without vector", e)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories
                    (namespace, repo_root, key, value, embedding, created_at, last_used_at, usage_count)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
                ON CONFLICT(namespace, repo_root, key) DO UPDATE SET
                    value = excluded.value,
                    embedding = excluded.embedding,
                    created_at = excluded.created_at
                """,
                (
                    namespace,
                    repo_root,
                    key,
                    value_json,
                    embedding_blob,
                    time.time(),
                ),
            )

    async def recall(
        self,
        namespace: str,
        query: str,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        if k <= 0:
            return []
        # 1. Пробуем vector search, если у нас есть embeddings.
        results = await self._recall_vector(namespace, query, k)
        if results:
            return results
        # 2. Fallback на FTS5.
        return self._recall_fts(namespace, query, k)

    async def forget_old(self, days: int = 90) -> int:
        if days < 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memories "
                "WHERE created_at < ? AND (last_used_at IS NULL OR last_used_at < ?)",
                (cutoff, cutoff),
            )
            return cur.rowcount or 0

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _recall_fts(
        self, namespace: str, query: str, k: int
    ) -> list[dict[str, Any]]:
        fts_q = _sanitize_fts_query(query)
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    SELECT m.id, m.namespace, m.key, m.value, m.created_at
                    FROM memories_fts f
                    JOIN memories m ON m.id = f.rowid
                    WHERE m.namespace = ? AND memories_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (namespace, fts_q, k),
                )
                rows = list(cur.fetchall())
            except sqlite3.OperationalError as e:
                logger.debug("FTS query failed (%s); falling back to LIKE", e)
                rows = list(
                    conn.execute(
                        """
                        SELECT id, namespace, key, value, created_at
                        FROM memories
                        WHERE namespace = ? AND value LIKE ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (namespace, f"%{query}%", k),
                    ).fetchall()
                )
        self._touch([r["id"] for r in rows])
        return [self._row_to_dict(r) for r in rows]

    async def _recall_vector(
        self, namespace: str, query: str, k: int
    ) -> list[dict[str, Any]]:
        if not self.embed_endpoint:
            return []
        try:
            qvec = await self._embed(query)
        except Exception as e:  # noqa: BLE001
            logger.debug("embed query failed: %s", e)
            return []
        if not qvec:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, namespace, key, value, embedding, created_at
                FROM memories
                WHERE namespace = ? AND embedding IS NOT NULL
                """,
                (namespace,),
            ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            try:
                vec = _unpack_floats(r["embedding"])
            except Exception:  # noqa: BLE001
                continue
            sim = _cosine(qvec, vec)
            scored.append((sim, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:k]
        self._touch([r["id"] for _, r in top])
        return [
            {**self._row_to_dict(r), "similarity": sim}
            for sim, r in top
            if sim > 0.0
        ]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            value = {"raw": row["value"]}
        return {
            "id": row["id"],
            "namespace": row["namespace"],
            "key": row["key"],
            "value": value,
            "created_at": row["created_at"],
        }

    def _touch(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE memories SET last_used_at = ?, usage_count = usage_count + 1 "
                f"WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )

    @staticmethod
    def _extract_embedding_text(value: dict[str, Any]) -> str:
        """Сериализация dict'а в текст для эмбеддинга."""
        pieces: list[str] = []
        for k, v in value.items():
            if isinstance(v, str):
                pieces.append(f"{k}: {v}")
            elif isinstance(v, (int, float, bool)):
                pieces.append(f"{k}: {v}")
            elif isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
                pieces.append(f"{k}: {', '.join(v)}")
            else:
                pieces.append(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)[:400]}")
        return "\n".join(pieces)[:8000]

    async def _embed(self, text: str) -> list[float] | None:
        """Запрос embedding через OpenAI-compatible endpoint."""
        if not self.embed_endpoint or not text.strip():
            return None
        try:
            import httpx
        except ImportError:
            return None
        headers = {"Content-Type": "application/json"}
        if self.embed_api_key:
            headers["Authorization"] = f"Bearer {self.embed_api_key}"
        body = {"model": self.embed_model, "input": text}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.embed_endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        try:
            return list(data["data"][0]["embedding"])
        except (KeyError, IndexError):
            return None


def _pack_floats(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_floats(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _sanitize_fts_query(q: str) -> str:
    """Минимальная защита от FTS5 query-syntax injection."""
    safe = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in q)
    tokens = [t for t in safe.split() if len(t) > 1]
    return " ".join(tokens) if tokens else q


__all__ = ["SqliteMemory"]
