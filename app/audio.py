"""Audio file storage.

Rules of the house:
  * audio is written under data/audio (mode 700) with files mode 600;
  * nothing in the web app serves these files;
  * every read and delete is written to audio_access_log.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import config, db


def _session_dir(session_id: str) -> Path:
    path = config.audio_dir() / session_id
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass
    return path


def save_chunk(session_id: str, chunk_index: int, data: bytes) -> Path:
    """Write one chunk to disk with owner-only permissions."""
    path = _session_dir(session_id) / f"{chunk_index:06d}.webm"
    tmp = path.with_suffix(".part")
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def read_audio(path: str, session_id: str | None, reason: str) -> bytes:
    """Read audio bytes and record the access. The only way audio leaves disk."""
    data = Path(path).read_bytes()
    log_access(path, session_id, "read", reason)
    return data


def delete_audio(path: str, session_id: str | None, reason: str) -> bool:
    """Delete one audio file, log it, and mark the chunk row as gone."""
    target = Path(path)
    existed = target.exists()
    if existed:
        try:
            target.unlink()
        except FileNotFoundError:
            existed = False
    if existed:
        log_access(str(target), session_id, "delete", reason)
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE chunks SET path = NULL, deleted_at = ? WHERE path = ?",
            (time.time(), str(target)),
        )
        conn.commit()
    finally:
        conn.close()
    return existed


def delete_session_audio(session_id: str, reason: str) -> int:
    """Delete every remaining chunk file of one session."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT path FROM chunks WHERE session_id = ? AND path IS NOT NULL",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    deleted = 0
    for row in rows:
        if delete_audio(row["path"], session_id, reason):
            deleted += 1
    return deleted


def cleanup_expired(retention_hours: float, now: float | None = None) -> int:
    """Scheduled cleanup: drop audio that is past its retention window.

    retention_hours == 0 means "as soon as the session has been transcribed",
    so only transcribed sessions are touched. > 0 uses the chunk's arrival time.
    """
    now = time.time() if now is None else now
    conn = db.connect()
    try:
        if retention_hours <= 0:
            rows = conn.execute(
                """
                SELECT c.path, c.session_id FROM chunks c
                JOIN sessions s ON s.id = c.session_id
                WHERE c.path IS NOT NULL AND s.status = 'transcribed'
                """
            ).fetchall()
        else:
            cutoff = now - retention_hours * 3600
            rows = conn.execute(
                "SELECT path, session_id FROM chunks WHERE path IS NOT NULL AND received_at < ?",
                (cutoff,),
            ).fetchall()
    finally:
        conn.close()

    deleted = 0
    for row in rows:
        if delete_audio(row["path"], row["session_id"], f"retention:{retention_hours}h"):
            deleted += 1
    return deleted


def log_access(path: str, session_id: str | None, action: str, reason: str) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO audio_access_log (path, session_id, action, reason, at) VALUES (?, ?, ?, ?, ?)",
            (path, session_id, action, reason, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
