"""faster-whisper wrapper (CPU, int8, VAD on).

The model is loaded lazily once per process so imports and tests stay cheap.
"""

from __future__ import annotations

import threading

from . import config

_model = None
_model_name: str | None = None
_lock = threading.Lock()


def get_model(name: str | None = None):
    """Load (or reuse) the whisper model."""
    global _model, _model_name
    cfg = config.load()["whisper"]
    name = name or cfg["model"]
    with _lock:
        if _model is not None and _model_name == name:
            return _model
        from faster_whisper import WhisperModel

        _model = WhisperModel(name, device=cfg["device"], compute_type=cfg["compute_type"])
        _model_name = name
        return _model


def transcribe_file(
    path: str,
    language: str | None = None,
    initial_prompt: str | None = None,
    model_name: str | None = None,
) -> dict:
    """Transcribe one audio file and return plain dicts for storage."""
    cfg = config.load()["whisper"]
    model = get_model(model_name)
    segments, info = model.transcribe(
        path,
        language=None if not language or language == "auto" else language,
        initial_prompt=initial_prompt or None,
        beam_size=cfg["beam_size"],
        vad_filter=cfg["vad_filter"],
    )
    collected = [
        {
            "text": segment.text.strip(),
            "start": float(segment.start),
            "end": float(segment.end),
            "avg_logprob": float(segment.avg_logprob),
            "no_speech_prob": float(segment.no_speech_prob),
            "compression_ratio": float(segment.compression_ratio),
        }
        for segment in segments
    ]
    return {
        "segments": collected,
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": getattr(info, "duration", None),
        "model": _model_name,
    }
