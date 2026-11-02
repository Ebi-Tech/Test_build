"""Gap events: heartbeats, visibility changes, wake lock, offline/online.

The client is expected to send a heartbeat every 30s while a session is open.
If a heartbeat arrives after a longer silence than `heartbeat_gap_seconds`, we
record a gap so "the phone stopped recording" is visible in the data instead of
silently missing.
"""

from __future__ import annotations

import json
import time

from . import db

HEARTBEAT = "heartbeat"


def record(
    shop_id: str | None,
    session_id: str | None,
    event_type: str,
    at: float,
    meta: dict | None = None,
    duration_s: float | None = None,
) -> None:
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO gap_events (shop_id, session_id, event_type, at, duration_s, meta)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (shop_id, session_id, event_type, at, duration_s, json.dumps(meta) if meta else None),
        )
        conn.commit()
    finally:
        conn.close()


def detect_gaps(timestamps: list[float], threshold_s: float) -> list[dict]:
    """Pure helper: silence longer than threshold between two heartbeats."""
    ordered = sorted(timestamps)
    gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        delta = current - previous
        if delta > threshold_s:
            gaps.append({"start": previous, "end": current, "duration": delta})
    return gaps


def handle_heartbeat(
    shop_id: str,
    session_id: str,
    at: float,
    threshold_s: float,
    record_event: bool = True,
) -> dict | None:
    """Store the heartbeat and log a gap when the previous one is too old."""
    conn = db.connect()
    try:
        last = conn.execute(
            """
            SELECT at FROM gap_events
            WHERE session_id = ? AND event_type = ?
            ORDER BY at DESC LIMIT 1
            """,
            (session_id, HEARTBEAT),
        ).fetchone()
    finally:
        conn.close()

    if record_event:
        record(shop_id, session_id, HEARTBEAT, at)

    if not last:
        return None
    duration = at - last["at"]
    if duration <= threshold_s:
        return None

    gap = {"start": last["at"], "end": at, "duration": duration}
    record(shop_id, session_id, "heartbeat_gap", at, meta=gap, duration_s=duration)
    return gap


def handle_event(shop_id: str, session_id: str, event_type: str, at: float, meta: dict | None) -> dict | None:
    """Route one client event. Returns a gap dict when a gap was detected."""
    cfg = _config()
    if event_type == HEARTBEAT:
        return handle_heartbeat(shop_id, session_id, at, cfg["heartbeat_gap_seconds"])
    record(shop_id, session_id, event_type, at, meta=meta)
    return None


def _config() -> dict:
    from . import config

    return config.load()


def events_for_session(session_id: str) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM gap_events WHERE session_id = ? ORDER BY at ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def now() -> float:
    return time.time()
