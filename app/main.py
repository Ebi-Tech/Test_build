"""Beyond Sale — minimal FastAPI backend.

Routes
  GET  /                     recorder page (static)
  GET  /config               public frontend settings (no tokens)
  POST /session/start        open a session, returns session_id
  POST /upload               one audio chunk (shop token required)
  POST /events               heartbeats + gap events (shop token required)
  POST /session/end          finish a session so it gets transcribed
  GET  /api/sessions         read-only transcripts (shop token required)

Audio is stored under data/audio and is deliberately NOT reachable over HTTP.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, auth, config, db, events, worker

STATIC_DIR = config.ROOT / "static"
_worker = worker.Worker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.ensure_shop_rows()
    if os.environ.get("BTS_WORKER", "1") != "0":
        _worker.start()
    try:
        yield
    finally:
        _worker.stop()


app = FastAPI(title="Beyond Sale", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def require_shop(shop_id: str, token: str) -> dict:
    shop = auth.authenticate(shop_id, token)
    if not shop:
        raise HTTPException(status_code=403, detail="invalid shop token")
    return shop


def require_session(session_id: str, shop_id: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    if not row or row["shop_id"] != shop_id:
        raise HTTPException(status_code=404, detail="unknown session")
    return dict(row)


# --------------------------------------------------------------------------- #
# pages and frontend config
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/config")
def frontend_config() -> JSONResponse:
    cfg = config.load()
    return JSONResponse(
        {
            "consent_text": cfg["consent_text"],
            "chunk_seconds": cfg["chunk_seconds"],
            "heartbeat_gap_seconds": cfg["heartbeat_gap_seconds"],
            "silence_threshold_db": cfg["silence_threshold_db"],
            "shop_ids": [shop["id"] for shop in cfg.get("shops", [])],
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Must be served from the root scope so the PWA can control "/".
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "time": time.time()}


# --------------------------------------------------------------------------- #
# sessions, chunks, events
# --------------------------------------------------------------------------- #


@app.post("/session/start")
def session_start(payload: dict[str, Any]) -> dict:
    shop_id = str(payload.get("shop_id", ""))
    require_shop(shop_id, str(payload.get("token", "")))

    session_id = uuid.uuid4().hex
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO sessions (id, shop_id, started_at, status) VALUES (?, ?, ?, 'recording')",
            (session_id, shop_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id}


@app.post("/session/end")
def session_end(payload: dict[str, Any]) -> dict:
    shop_id = str(payload.get("shop_id", ""))
    require_shop(shop_id, str(payload.get("token", "")))
    session_id = str(payload.get("session_id", ""))
    session = require_session(session_id, shop_id)

    conn = db.connect()
    try:
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
        if session["status"] in ("recording", "ready") and chunk_count:
            conn.execute(
                "UPDATE sessions SET status = 'ready', ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )
        elif not chunk_count:
            conn.execute(
                "UPDATE sessions SET status = 'empty', ended_at = ? WHERE id = ?",
                (time.time(), session_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "session_id": session_id}


@app.post("/upload")
def upload(
    shop_id: str = Form(...),
    token: str = Form(...),
    session_id: str = Form(...),
    chunk_index: int = Form(...),
    start_timestamp: float = Form(...),
    level_db: float | None = Form(None),
    audio_file: UploadFile = File(..., alias="audio"),
) -> dict:
    require_shop(shop_id, token)
    session = require_session(session_id, shop_id)
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="chunk_index must be >= 0")

    cfg = config.load()
    data = audio_file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty chunk")
    if len(data) > int(cfg["max_chunk_bytes"]):
        raise HTTPException(status_code=413, detail="chunk too large")

    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT id FROM chunks WHERE session_id = ? AND chunk_index = ?",
            (session_id, chunk_index),
        ).fetchone()
        if existing:
            # Offline retry: same chunk again. Idempotent by design.
            return {"status": "duplicate", "chunk_index": chunk_index}

        path = audio.save_chunk(session_id, chunk_index, data)
        conn.execute(
            """
            INSERT INTO chunks (session_id, chunk_index, start_timestamp, received_at,
                                byte_size, level_db, path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, chunk_index, start_timestamp, time.time(), len(data), level_db, str(path)),
        )
        conn.execute(
            "UPDATE sessions SET chunk_count = (SELECT COUNT(*) FROM chunks WHERE session_id = ?) WHERE id = ?",
            (session_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()

    # A chunk that arrives after transcription means we lost audio mid-session.
    if session["status"] == "transcribed":
        conn = db.connect()
        try:
            gone = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE session_id = ? AND path IS NULL",
                (session_id,),
            ).fetchone()["n"]
            if gone == 0:
                conn.execute("UPDATE sessions SET status = 'ready' WHERE id = ?", (session_id,))
                conn.commit()
        finally:
            conn.close()
    return {"status": "stored", "chunk_index": chunk_index, "bytes": len(data)}


class EventPayload(BaseModel):
    shop_id: str
    token: str
    session_id: str
    type: str
    at: float
    meta: dict[str, Any] | None = None


@app.post("/events")
def post_event(payload: EventPayload) -> dict:
    require_shop(payload.shop_id, payload.token)
    require_session(payload.session_id, payload.shop_id)
    gap = events.handle_event(
        payload.shop_id, payload.session_id, payload.type, payload.at, payload.meta
    )
    return {"ok": True, "gap": gap}


@app.get("/api/sessions")
def api_sessions(
    shop_id: str = Query(...), token: str = Query(...), limit: int = Query(50, le=200)
) -> dict:
    require_shop(shop_id, token)
    conn = db.connect()
    try:
        sessions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sessions WHERE shop_id = ? ORDER BY started_at DESC LIMIT ?",
                (shop_id, limit),
            ).fetchall()
        ]
        for session in sessions:
            session["segments"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT segment_index, text, start, \"end\", avg_logprob, no_speech_prob, "
                    "compression_ratio, language, model, noise_indicator "
                    "FROM segments WHERE session_id = ? ORDER BY segment_index",
                    (session["id"],),
                ).fetchall()
            ]
            session["events"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT event_type, at, duration_s, meta FROM gap_events "
                    "WHERE session_id = ? ORDER BY at",
                    (session["id"],),
                ).fetchall()
            ]
    finally:
        conn.close()
    return {"shops": [], "sessions": sessions}


# Anything under /static is a real file; audio is elsewhere and has no route.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
