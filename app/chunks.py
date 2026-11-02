"""Chunk ordering and reassembly.

MediaRecorder gives us one WebM stream split at arbitrary byte offsets every
`timeslice` seconds: only the first chunk carries the container header, so
chunks are concatenated back together before transcription.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import audio, db


@dataclass
class Chunk:
    chunk_index: int
    start_timestamp: float  # ms since recording start
    path: str
    level_db: float | None


def load_chunks(session_id: str, include_deleted: bool = False) -> list[Chunk]:
    """All chunks of a session, ordered by chunk_index (not arrival order)."""
    sql = "SELECT chunk_index, start_timestamp, path, level_db FROM chunks WHERE session_id = ?"
    if not include_deleted:
        sql += " AND path IS NOT NULL"
    sql += " ORDER BY chunk_index ASC"
    conn = db.connect()
    try:
        rows = conn.execute(sql, (session_id,)).fetchall()
    finally:
        conn.close()
    return [Chunk(r["chunk_index"], r["start_timestamp"], r["path"], r["level_db"]) for r in rows]


def order_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Sort by chunk_index and drop duplicate indexes (retries can repeat one)."""
    ordered: list[Chunk] = []
    seen: set[int] = set()
    for chunk in sorted(chunks, key=lambda c: c.chunk_index):
        if chunk.chunk_index in seen:
            continue
        seen.add(chunk.chunk_index)
        ordered.append(chunk)
    return ordered


def missing_indices(chunks: list[Chunk]) -> list[int]:
    """Indexes that never arrived, so a future experiment can measure holes."""
    if not chunks:
        return []
    present = {c.chunk_index for c in chunks}
    last = max(present)
    return [i for i in range(0, last + 1) if i not in present]


def reassemble(session_id: str, out_path: Path) -> tuple[Path, int]:
    """Concatenate the session's chunks in order into one file.

    Returns (path, byte_count). Reads go through audio.read_audio so the
    access log records them.
    """
    chunks = order_chunks(load_chunks(session_id))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out_path, "wb") as out:
        for chunk in chunks:
            data = audio.read_audio(chunk.path, session_id, "reassembly")
            out.write(data)
            total += len(data)
    try:
        out_path.chmod(0o600)
    except PermissionError:
        pass
    return out_path, total


def chunk_windows(chunks: list[Chunk]) -> list[tuple[float, float, float | None]]:
    """Turn chunk start offsets into (start_s, end_s, level_db) audio windows.

    Times are relative to the first chunk, matching the reassembled file.
    """
    ordered = order_chunks(chunks)
    if not ordered:
        return []
    base = ordered[0].start_timestamp
    starts = [(c.start_timestamp - base) / 1000.0 for c in ordered]
    windows = []
    for i, chunk in enumerate(ordered):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else start + 30.0
        windows.append((start, end, chunk.level_db))
    return windows


def noise_for_window(windows: list[tuple[float, float, float | None]], start: float, end: float) -> float | None:
    """Rough noise indicator: mean client level of chunks overlapping a segment."""
    levels = [
        level
        for (w_start, w_end, level) in windows
        if level is not None and w_end > start and w_start < end
    ]
    if not levels:
        return None
    return round(sum(levels) / len(levels), 1)
