"""SQLite storage. One connection per call; the app is single-user and local."""

from __future__ import annotations

import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    token          TEXT NOT NULL,
    initial_prompt TEXT,
    language       TEXT DEFAULT 'auto',          -- 'auto' or 'en', 'nl', ...
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    shop_id        TEXT NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL,
    status         TEXT NOT NULL DEFAULT 'recording',  -- recording|ready|transcribing|transcribed|empty|error
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    transcribed_at REAL,
    model          TEXT,
    language       TEXT,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    start_timestamp REAL NOT NULL,   -- ms since recording start
    received_at     REAL NOT NULL,
    byte_size       INTEGER NOT NULL,
    level_db        REAL,            -- client measured level of this chunk
    path            TEXT,            -- NULL once the audio has been deleted
    deleted_at      REAL,
    UNIQUE (session_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS segments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    shop_id           TEXT NOT NULL,
    segment_index     INTEGER NOT NULL,
    text              TEXT NOT NULL,
    start             REAL NOT NULL,
    "end"             REAL NOT NULL,
    avg_logprob       REAL,
    no_speech_prob    REAL,
    compression_ratio REAL,
    language          TEXT,
    model             TEXT,
    noise_indicator   REAL,          -- mean chunk level (dBFS) over the segment; rough
    created_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id    TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,
    at         REAL NOT NULL,
    duration_s REAL,
    meta       TEXT
);

CREATE TABLE IF NOT EXISTS audio_access_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL,
    session_id TEXT,
    action     TEXT NOT NULL,   -- read | delete
    reason     TEXT,
    at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    shop_id    TEXT,
    rating     INTEGER,
    note       TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks (session_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_segments_session ON segments (session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON gap_events (session_id, at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def ensure_shop_rows() -> None:
    """Mirror the config file's shops into the table (idempotent)."""
    conn = connect()
    try:
        for entry in config.load().get("shops", []):
            if not entry.get("id"):
                continue
            conn.execute(
                """
                INSERT INTO shops (id, name, token, initial_prompt, language, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    token = excluded.token,
                    initial_prompt = excluded.initial_prompt,
                    language = excluded.language
                """,
                (
                    entry["id"],
                    entry.get("name", entry["id"]),
                    entry.get("token", ""),
                    entry.get("initial_prompt"),
                    entry.get("language", "auto"),
                    time.time(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
