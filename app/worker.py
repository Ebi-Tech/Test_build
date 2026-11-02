"""Background transcription worker.

One thread, one job at a time — this is a CPU laptop backend, not a cluster.

Loop:
  1. sweep sessions that stopped sending chunks into `ready`;
  2. claim the oldest `ready` session;
  3. reassemble its chunks, transcribe with faster-whisper + vad_filter,
     store segments (with confidence fields), drop audio per retention;
  4. every few minutes, run the retention cleanup.
"""

from __future__ import annotations

import threading
import time
import traceback

from . import audio, chunks, config, db, transcribe

CLEANUP_INTERVAL_S = 300


def sweep_idle_sessions(idle_seconds: float) -> int:
    """Sessions whose last chunk is older than idle_seconds are finished."""
    cutoff = time.time() - idle_seconds
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT s.id, MAX(c.received_at) AS last_chunk
            FROM sessions s JOIN chunks c ON c.session_id = s.id
            WHERE s.status = 'recording'
            GROUP BY s.id
            HAVING last_chunk < ?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE sessions SET status = 'ready', ended_at = COALESCE(ended_at, ?) WHERE id = ?",
                (row["last_chunk"], row["id"]),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def claim_next_session() -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE status = 'ready' ORDER BY started_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE sessions SET status = 'transcribing' WHERE id = ?", (row["id"],))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def _set_status(session_id: str, status: str, error: str | None = None, **fields) -> None:
    assignments = ["status = ?"]
    values: list = [status]
    if error is not None:
        assignments.append("error = ?")
        values.append(error)
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(session_id)
    conn = db.connect()
    try:
        conn.execute(f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def process_session(session: dict) -> dict:
    """Transcribe one claimed session. Returns a small result summary."""
    session_id = session["id"]
    shop = config.shop(session["shop_id"]) or {}
    store = config.load()
    retention_hours = float(store["retention_hours"])

    chunk_rows = chunks.load_chunks(session_id)
    if not chunk_rows:
        _set_status(session_id, "empty", error="no chunks uploaded")
        return {"session_id": session_id, "segments": 0, "status": "empty"}

    tmp_path = config.tmp_dir() / f"{session_id}.webm"
    try:
        chunks.reassemble(session_id, tmp_path)
        result = transcribe.transcribe_file(
            str(tmp_path),
            language=shop.get("language"),
            initial_prompt=shop.get("initial_prompt"),
        )
        windows = chunks.chunk_windows(chunk_rows)
        conn = db.connect()
        try:
            conn.execute("DELETE FROM segments WHERE session_id = ?", (session_id,))
            for index, segment in enumerate(result["segments"]):
                conn.execute(
                    """
                    INSERT INTO segments (
                        session_id, shop_id, segment_index, text, start, "end",
                        avg_logprob, no_speech_prob, compression_ratio,
                        language, model, noise_indicator, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        session["shop_id"],
                        index,
                        segment["text"],
                        segment["start"],
                        segment["end"],
                        segment["avg_logprob"],
                        segment["no_speech_prob"],
                        segment["compression_ratio"],
                        result["language"],
                        result["model"],
                        chunks.noise_for_window(windows, segment["start"], segment["end"]),
                        time.time(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        _set_status(
            session_id,
            "transcribed",
            transcribed_at=time.time(),
            model=result["model"],
            language=result["language"],
        )

        # retention 0 = audio is not kept once the transcript exists
        if retention_hours <= 0:
            audio.cleanup_expired(0)

        return {
            "session_id": session_id,
            "segments": len(result["segments"]),
            "status": "transcribed",
            "language": result["language"],
        }
    except Exception as exc:  # keep the audio so it can be retried
        _set_status(session_id, "error", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return {"session_id": session_id, "segments": 0, "status": "error", "error": str(exc)}
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


class Worker:
    """Small polling thread; start() and stop() are safe to call once."""

    def __init__(self, poll_seconds: float = 3.0) -> None:
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bts-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def tick(self) -> dict | None:
        """One pass of the loop (also called directly by tests)."""
        cfg = config.load()
        sweep_idle_sessions(float(cfg["session_idle_seconds"]))

        if time.time() - self._last_cleanup > CLEANUP_INTERVAL_S:
            self._last_cleanup = time.time()
            audio.cleanup_expired(float(cfg["retention_hours"]))

        session = claim_next_session()
        if not session:
            return None
        return process_session(session)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.tick()
                if result is None:
                    self._stop.wait(self.poll_seconds)
            except Exception:
                traceback.print_exc()
                self._stop.wait(self.poll_seconds)
