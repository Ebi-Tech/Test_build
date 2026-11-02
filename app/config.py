"""Configuration and filesystem layout.

Everything comes from one JSON file (copy config/config.example.json to
config/config.json and edit it). A handful of environment variables override
the most useful knobs when you do not want to touch the file.

Paths are resolved on every call so tests can point the app at a temp dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    # Shown on the consent screen (first use only).
    "consent_text": (
        "This shop records short spoken conversations so staff can review what "
        "was said later. Recording only happens while the RECORD button is on. "
        "There is a visible paused state, and you can ask for a recording to be "
        "deleted at any time. Audio is removed automatically after it has been "
        "transcribed."
    ),
    # 0 = delete audio as soon as the session has been transcribed.
    "retention_hours": 0,
    # A heartbeat missing for longer than this is logged as a gap.
    "heartbeat_gap_seconds": 90,
    # A session with no new chunk for this long is treated as finished.
    "session_idle_seconds": 900,
    # MediaRecorder timeslice in seconds (also used as "what is one chunk").
    "chunk_seconds": 30,
    # Client-side silence skip. null disables the feature.
    "silence_threshold_db": -55,
    "max_chunk_bytes": 8 * 1024 * 1024,
    "whisper": {
        "model": "small",  # tiny / base / small / medium / large-v3
        "device": "cpu",
        "compute_type": "int8",
        "beam_size": 1,
        "vad_filter": True,  # faster-whisper built-in VAD
    },
    "shops": [],
}


def config_path() -> Path:
    """BTS_CONFIG, else config/config.json, else the checked-in example."""
    explicit = os.environ.get("BTS_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    preferred = ROOT / "config" / "config.json"
    if preferred.exists():
        return preferred
    return ROOT / "config" / "config.example.json"


def load() -> dict:
    """Read the config file, layered over DEFAULTS. Never raises on a bad file."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    path = config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
        for key, value in raw.items():
            if key == "whisper" and isinstance(value, dict):
                cfg["whisper"].update(value)
            else:
                cfg[key] = value

    # Environment overrides, handy for experiments.
    if "RETENTION_HOURS" in os.environ:
        cfg["retention_hours"] = float(os.environ["RETENTION_HOURS"])
    if "WHISPER_MODEL" in os.environ:
        cfg["whisper"]["model"] = os.environ["WHISPER_MODEL"]
    if "WHISPER_DEVICE" in os.environ:
        cfg["whisper"]["device"] = os.environ["WHISPER_DEVICE"]
    if "WHISPER_COMPUTE_TYPE" in os.environ:
        cfg["whisper"]["compute_type"] = os.environ["WHISPER_COMPUTE_TYPE"]
    return cfg


def shop(shop_id: str) -> dict | None:
    """Look up one shop (including its token) by id."""
    for entry in load().get("shops", []):
        if entry.get("id") == shop_id:
            return entry
    return None


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except PermissionError:
        pass
    return path


def data_dir() -> Path:
    return _private_dir(Path(os.environ.get("BTS_DATA_DIR", ROOT / "data")).expanduser())


def audio_dir() -> Path:
    """Audio lives here: outside static/, never mapped to a web route."""
    return _private_dir(data_dir() / "audio")


def tmp_dir() -> Path:
    return _private_dir(data_dir() / "tmp")


def db_path() -> Path:
    return data_dir() / "beyond_the_sale.sqlite3"
