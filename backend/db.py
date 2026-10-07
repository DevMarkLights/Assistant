# SQLite-backed persistence layer for conversations and exchanges.
# Stores message embeddings as binary blobs and performs cosine similarity search in-process via NumPy.
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DEFAULT_DIR = Path.home() / "Library/Application Support/PersonalAssistant"
DB_PATH = Path(os.getenv("ASSISTANT_DB", DEFAULT_DIR / "memory.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchanges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_text       TEXT NOT NULL,
    assistant_text  TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    embed_model     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchanges_conv ON exchanges(conversation_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def to_blob(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def create_conversation(title: str | None = None) -> dict:
    conv = {"id": uuid.uuid4().hex, "title": title, "created_at": now()}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES (:id, :title, :created_at)",
            conv,
        )
    return conv


def list_conversations() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at FROM conversations ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_exchange(conversation_id: str, user_text: str, assistant_text: str,
                 embedding: list[float], embed_model: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO exchanges
               (conversation_id, user_text, assistant_text, embedding, embed_model, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conversation_id, user_text, assistant_text, to_blob(embedding), embed_model, now()),
        )
        return cur.lastrowid


def list_exchanges(conversation_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, user_text, assistant_text, created_at
               FROM exchanges WHERE conversation_id = ? ORDER BY id""",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def search_exchanges(query_vec: list[float], embed_model: str, top_k: int = 4,
                     min_similarity: float = 0.5, exclude_ids=()) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, conversation_id, user_text, assistant_text, created_at, embedding
               FROM exchanges WHERE embed_model = ?""",
            (embed_model,),
        ).fetchall()

    excluded = set(exclude_ids)
    rows = [r for r in rows if r["id"] not in excluded]
    if not rows:
        return []

    matrix = np.stack([from_blob(r["embedding"]) for r in rows])
    q = np.asarray(query_vec, dtype=np.float32)
    scores = matrix @ q / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(q))

    best = np.argsort(scores)[::-1][:top_k]

    results = []
    for i in best:
        if scores[i] < min_similarity:
            break
        r = rows[i]
        results.append({
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "user_text": r["user_text"],
            "assistant_text": r["assistant_text"],
            "created_at": r["created_at"],
            "score": round(float(scores[i]), 3),
        })
    return results

def get_conversation(conversation_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    return dict(row) if row else None

def delete_conversation(conversation_id: str) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM exchanges WHERE conversation_id = ?", (conversation_id,))
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cur.rowcount > 0

