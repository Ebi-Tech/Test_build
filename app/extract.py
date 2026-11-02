"""Next step: turn a transcript into structured events.

STUB — intentionally not implemented yet. Keep the signature stable: the
transcription pipeline will call this once there is a policy for what counts as
an event (product mentioned, objection, alternative offered, close attempt...).
"""

from __future__ import annotations


def extract_events(transcript: list[dict]) -> list[dict]:
    """Extract structured events from transcript segments.

    Args:
        transcript: list of segment dicts as stored in the `segments` table
            (text, start, end, avg_logprob, no_speech_prob, compression_ratio,
            language, model, noise_indicator).

    Returns:
        list of events. Not implemented yet.

    TODO(next step):
      * decide the event taxonomy and the annotation policy;
      * cheap rules first (keyword/alias matching from shop config), then
        model-assisted extraction if a local model is available;
      * always keep segment references (start/end) so events are auditable.
    """
    raise NotImplementedError("extract_events is a stub; event extraction comes next")
