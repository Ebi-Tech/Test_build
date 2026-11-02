"""Small self-contained test suite.

    .venv/bin/python tests/test_all.py

Prints one ok/FAILED line per check and "all checks passed" at the end.
The whisper model is never loaded here: the transcription pipeline itself is
covered by running the app, not by these checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bts-test-"))
TOKEN = "test-token-abc"
SHOP = "demo-shop"

# Environment must be set before the app is imported.
os.environ["BTS_DATA_DIR"] = str(TMP / "data")
os.environ["BTS_CONFIG"] = str(TMP / "config.json")
os.environ["BTS_WORKER"] = "0"
(TMP / "config.json").write_text(
    json.dumps(
        {
            "consent_text": "Test consent text.",
            "retention_hours": 0,
            "heartbeat_gap_seconds": 90,
            "chunk_seconds": 30,
            "silence_threshold_db": -55,
            "whisper": {"model": "tiny", "device": "cpu", "compute_type": "int8"},
            "shops": [
                {
                    "id": SHOP,
                    "name": "Demo Shop",
                    "token": TOKEN,
                    "language": "auto",
                    "initial_prompt": "City Cruiser, Trail Pro 2",
                }
            ],
        }
    ),
    encoding="utf-8",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import audio, chunks, config, db, events, main  # noqa: E402

client = TestClient(main.app)
client.__enter__()  # run lifespan: create schema + shops

failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the harness reports everything
        failures.append(name)
        print(f"FAILED - {name}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok - {name}")


def make_session(status: str = "recording") -> str:
    session_id = f"sess-{int(time.time() * 1000)}-{os.urandom(3).hex()}"
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO sessions (id, shop_id, started_at, status) VALUES (?, ?, ?, ?)",
            (session_id, SHOP, time.time(), status),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def add_chunk(session_id: str, index: int, payload: bytes, start_ms: float, level: float | None = -30.0, received_at: float | None = None) -> Path:
    path = audio.save_chunk(session_id, index, payload)
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO chunks (session_id, chunk_index, start_timestamp, received_at, byte_size, level_db, path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, index, start_ms, received_at or time.time(), len(payload), level, str(path)),
        )
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------- #
# 1. chunk ordering and reassembly
# --------------------------------------------------------------------------- #
def test_reassembly() -> None:
    session_id = make_session()
    # Deliberately out of order on disk, plus a duplicate index from an upload retry.
    add_chunk(session_id, 2, b"[third]", 60000)
    add_chunk(session_id, 0, b"[first]", 0)
    add_chunk(session_id, 1, b"[second]", 30000)
    add_chunk(session_id, 1, b"[second]", 30000)  # ignored: duplicate index

    out = TMP / "reassembled.webm"
    _, size = chunks.reassemble(session_id, out)
    data = out.read_bytes()
    assert data == b"[first][second][third]", data
    assert size == len(data)

    ordered = chunks.order_chunks(chunks.load_chunks(session_id))
    assert [c.chunk_index for c in ordered] == [0, 1, 2], [c.chunk_index for c in ordered]

    # A hole in the sequence is reported, not ignored.
    gap_session = make_session()
    add_chunk(gap_session, 0, b"a", 0)
    add_chunk(gap_session, 2, b"c", 60000)
    assert chunks.missing_indices(chunks.load_chunks(gap_session)) == [1]

    # Chunk windows + rough noise indicator.
    windows = chunks.chunk_windows(chunks.load_chunks(session_id))
    assert windows[0][0] == 0.0 and windows[0][1] == 30.0, windows
    assert chunks.noise_for_window(windows, 2.0, 5.0) == -30.0


# --------------------------------------------------------------------------- #
# 2. offline queue retry (browser code, run under node)
# --------------------------------------------------------------------------- #
def test_offline_queue() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "queue_test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node test failed:\n{result.stdout}\n{result.stderr}")
    assert "ok - offline queue retry" in result.stdout, result.stdout


# --------------------------------------------------------------------------- #
# 3. retention cleanup deletes audio
# --------------------------------------------------------------------------- #
def test_retention() -> None:
    session_id = make_session(status="transcribed")
    path = add_chunk(session_id, 0, b"audio-bytes", 0)

    assert path.exists()
    deleted = audio.cleanup_expired(0)
    assert deleted >= 1, deleted
    assert not path.exists(), "audio should be deleted once the session is transcribed"

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT path, deleted_at FROM chunks WHERE session_id = ?", (session_id,)
        ).fetchone()
        logged = conn.execute(
            "SELECT COUNT(*) AS n FROM audio_access_log WHERE path = ? AND action = 'delete'",
            (str(path),),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert row["path"] is None and row["deleted_at"] is not None, dict(row)
    assert logged == 1, logged

    # retention > 0 keeps recent audio and deletes old audio
    fresh_session = make_session(status="transcribed")
    fresh = add_chunk(fresh_session, 0, b"recent", 0, received_at=time.time())
    old_session = make_session(status="transcribed")
    old = add_chunk(old_session, 0, b"old", 0, received_at=time.time() - 5 * 3600)

    audio.cleanup_expired(2)
    assert fresh.exists(), "audio inside the retention window must stay"
    assert not old.exists(), "audio older than the retention window must go"

    # A recording session's audio is never deleted by the retention=0 sweep
    recording_session = make_session(status="recording")
    live = add_chunk(recording_session, 0, b"live", 0)
    audio.cleanup_expired(0)
    assert live.exists(), "in-progress audio must not be cleaned up"
    live.unlink()


# --------------------------------------------------------------------------- #
# 4. no audio download route exists
# --------------------------------------------------------------------------- #
def test_no_audio_route() -> None:
    configured = {getattr(route, "path", "") for route in main.app.routes}
    assert "/upload" in configured
    for path in configured:
        lowered = path.lower()
        assert "audio" not in lowered, f"route {path} looks like it serves audio"
        assert "download" not in lowered, f"route {path} looks like a download"

    static_dir = (Path(main.STATIC_DIR)).resolve()
    audio_dir = config.audio_dir().resolve()
    assert static_dir not in audio_dir.parents, "audio must live outside static/"

    session_id = make_session()
    path = add_chunk(session_id, 0, b"secret", 0)
    relative = path.relative_to(config.audio_dir())
    for candidate in (
        f"/audio/{session_id}/{relative.name}",
        f"/data/audio/{session_id}/{relative.name}",
        f"/static/../data/audio/{session_id}/{relative.name}",
        f"/static/{relative.name}",
    ):
        response = client.get(candidate)
        assert response.status_code == 404, f"{candidate} -> {response.status_code}"
        assert b"secret" not in response.content


# --------------------------------------------------------------------------- #
# 5. token check rejects bad tokens
# --------------------------------------------------------------------------- #
def test_tokens() -> None:
    bad = client.post("/session/start", json={"shop_id": SHOP, "token": "nope"})
    assert bad.status_code == 403, bad.status_code

    unknown_shop = client.post("/session/start", json={"shop_id": "ghost", "token": TOKEN})
    assert unknown_shop.status_code == 403, unknown_shop.status_code

    good = client.post("/session/start", json={"shop_id": SHOP, "token": TOKEN})
    assert good.status_code == 200, good.text
    session_id = good.json()["session_id"]

    upload_fields = {
        "shop_id": SHOP,
        "token": "wrong-token",
        "session_id": session_id,
        "chunk_index": "0",
        "start_timestamp": "0",
        "level_db": "-40",
    }
    rejected = client.post(
        "/upload",
        data=upload_fields,
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
    )
    assert rejected.status_code == 403, rejected.status_code

    events_rejected = client.post(
        "/events",
        json={"shop_id": SHOP, "token": "wrong", "session_id": session_id, "type": "heartbeat", "at": time.time()},
    )
    assert events_rejected.status_code == 403, events_rejected.status_code

    # Good token: stored, idempotent on retry, file is owner-only.
    accepted = client.post(
        "/upload",
        data={**upload_fields, "token": TOKEN},
        files={"audio": ("chunk.webm", b"chunk-bytes-1", "audio/webm")},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "stored", accepted.json()

    duplicate = client.post(
        "/upload",
        data={**upload_fields, "token": TOKEN},
        files={"audio": ("chunk.webm", b"chunk-bytes-1", "audio/webm")},
    )
    assert duplicate.json()["status"] == "duplicate", duplicate.json()

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT path FROM chunks WHERE session_id = ? AND chunk_index = 0", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    mode = os.stat(row["path"]).st_mode & 0o777
    assert mode == 0o600, oct(mode)


# --------------------------------------------------------------------------- #
# 6. heartbeat gap detection
# --------------------------------------------------------------------------- #
def test_heartbeat_gap() -> None:
    gaps = events.detect_gaps([0, 30, 60, 300], 90)
    assert len(gaps) == 1 and gaps[0]["duration"] == 240, gaps

    session_id = make_session()
    now = 1_700_000_000.0
    first = events.handle_heartbeat(SHOP, session_id, now, 90)
    assert first is None, "first heartbeat has nothing to compare to"
    second = events.handle_heartbeat(SHOP, session_id, now + 30, 90)
    assert second is None, "a normal 30s beat is not a gap"
    gap = events.handle_heartbeat(SHOP, session_id, now + 300, 90)
    assert gap is not None and int(gap["duration"]) == 270, gap

    stored = events.events_for_session(session_id)
    kinds = [row["event_type"] for row in stored]
    assert kinds.count("heartbeat") == 3, kinds
    assert "heartbeat_gap" in kinds, kinds

    # Client gap events land in the same table.
    response = client.post(
        "/events",
        json={
            "shop_id": SHOP,
            "token": TOKEN,
            "session_id": session_id,
            "type": "visibility_hidden",
            "at": now + 320,
            "meta": {"reason": "test"},
        },
    )
    assert response.status_code == 200, response.text
    assert "visibility_hidden" in [row["event_type"] for row in events.events_for_session(session_id)]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
check("chunk ordering and reassembly", test_reassembly)
check("offline queue retry", test_offline_queue)
check("retention cleanup deletes audio", test_retention)
check("no audio download route exists", test_no_audio_route)
check("token check rejects bad tokens", test_tokens)
check("heartbeat gap detection", test_heartbeat_gap)

client.__exit__(None, None, None)

if failures:
    print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)

print("\nall checks passed")
