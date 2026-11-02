# Beyond Sale

A small, free MVP: a phone browser records a shop conversation, uploads the audio
in 30-second chunks, a CPU backend transcribes it with faster-whisper, and the
transcript is stored in SQLite. No paid services, no cloud APIs, no build step.

> Audio is deleted as soon as the transcript exists (configurable). Nothing in the
> app can serve or download audio.

---

## What is in here

```
app/                  FastAPI backend
  main.py             routes: pages, /upload, /events, sessions
  config.py           config file loading, paths, env overrides
  db.py               SQLite schema (shops, sessions, chunks, segments,
                      gap_events, audio_access_log, feedback)
  auth.py             per-shop token check
  audio.py            chunk storage, access log, retention cleanup
  chunks.py           ordering, reassembly, chunk time windows, noise estimate
  transcribe.py       faster-whisper wrapper (lazy, CPU int8, vad_filter)
  worker.py           background worker: reassemble -> transcribe -> store
  events.py           heartbeats, gap detection, event log
  extract.py          STUB: extract_events(transcript) for the next step
static/               recorder page (plain HTML/CSS/JS) + PWA shell
  index.html          consent screen, shop setup, recorder UI
  app.js              MediaRecorder, heartbeat, wake lock, status light
  queue.js            IndexedDB-backed upload queue with backoff
  sw.js               service worker (app shell only)
  manifest.webmanifest
config/
  config.example.json copy to config/config.json and edit
tests/
  test_all.py         python checks + prints ok/FAILED
  queue_test.mjs      node check for the offline queue
data/                 created at runtime, git-ignored
  beyond_the_sale.sqlite3
  audio/<session>/    chunk files, mode 600, directory mode 700
  tmp/                reassembled file, deleted after transcription
```

## Run it

Requires Python 3.10+ (tested on 3.12) and ~1 GB of disk for the whisper model.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/config.example.json config/config.json
# edit config/config.json: set a long random shop token and your product names

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000> on the laptop: it asks for consent, then for the shop
token. The first transcription downloads the whisper model (`small`) and takes
noticeably longer than later ones.

### Expose it to the phone (Cloudflare Tunnel, not automated)

The phone needs HTTPS for the microphone and wake lock, and `localhost` is not the
phone. Use a quick tunnel in a second terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://<random>.trycloudflare.com` URL; open that on the phone,
accept consent, enter the shop token, then "Add to Home Screen" to install the PWA.
Quick tunnels are ephemeral — for a stable hostname, create a named tunnel with
`cloudflared tunnel create beyond-the-sale` and route a hostname to it.

The tunnel is deliberately manual: the app never touches Cloudflare credentials.
Keep `--host 127.0.0.1` so the app itself is not exposed on the LAN.

### Using the recorder

1. **Start recording** — asks for the mic, starts `MediaRecorder` with Opus at
   ~16 kbps and a 30-second timeslice.
2. Each chunk is written to IndexedDB *first*, then uploaded. The status light is
   grey (ready), green (recording), amber (paused), blue (uploading) or red
   (offline). Counters show chunks sent and chunks still queued.
3. **Pause** calls `MediaRecorder.pause()` — the mic really stops and a visible
   "Paused" banner appears.
4. **Stop & save** closes the session; the worker then reassembles and transcribes it.
   If you never press stop, a session becomes `ready` after `session_idle_seconds`
   without new chunks.

Losing the network does not lose audio: chunks stay in IndexedDB and are retried
with exponential backoff on the next successful flush or `online` event.

## Config

`config/config.json` (the checked-in `config.example.json` is the same shape):

| key | meaning |
| --- | --- |
| `consent_text` | text of the first-use consent screen |
| `retention_hours` | `0` = delete audio right after a successful transcription; `>0` = keep audio that long, then a scheduled cleanup removes it |
| `heartbeat_gap_seconds` | a missing heartbeat longer than this is stored as a `heartbeat_gap` event (default 90) |
| `session_idle_seconds` | no new chunk for this long ⇒ session is treated as finished |
| `chunk_seconds` | timeslice used by the browser |
| `silence_threshold_db` | client-side silence skip threshold in dBFS; `null` disables it |
| `max_chunk_bytes` | upload size guard |
| `whisper.model` | `tiny`/`base`/`small` (default)/`medium`/`large-v3` |
| `whisper.device`, `whisper.compute_type` | `cpu` + `int8` by default |
| `whisper.beam_size`, `whisper.vad_filter` | decoding options; VAD is faster-whisper's built-in |
| `shops[].id`, `shops[].name` | shop identity |
| `shops[].token` | the upload token for that shop (client enters it once per phone) |
| `shops[].initial_prompt` | optional prompt with product names to bias recognition |
| `shops[].language` | `auto` (default) or e.g. `en`, `nl` |

Environment overrides for experiments: `RETENTION_HOURS`, `WHISPER_MODEL`,
`WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, plus `BTS_CONFIG` and `BTS_DATA_DIR`.

## API

| route | auth | purpose |
| --- | --- | --- |
| `GET /` | – | recorder page |
| `GET /config` | – | consent text + shop ids (never tokens) |
| `POST /session/start` | shop token | returns `session_id` |
| `POST /upload` | shop token | multipart chunk: `shop_id, token, session_id, chunk_index, start_timestamp (ms), level_db?, audio` |
| `POST /events` | shop token | `{shop_id, token, session_id, type, at, meta?}` |
| `POST /session/end` | shop token | mark the session finished |
| `GET /api/sessions` | shop token | sessions with segments and events |
| `POST /feedback` *(stub table)* | – | not exposed yet; `feedback` table exists for later |

There is intentionally **no** route that reads or serves audio files. Every read of
an audio file happens inside the worker and is written to `audio_access_log`.

## Data

Per segment we keep what a later offline comparison of policies needs:
`text`, `start`, `end`, `avg_logprob`, `no_speech_prob`, `compression_ratio`,
`language`, `model`, and `noise_indicator` (mean client-measured dBFS of the
overlapping chunks — rough on purpose).

Read a transcript with the API:

```bash
curl -s "http://localhost:8000/api/sessions?shop_id=demo-shop&token=YOUR_TOKEN" | python3 -m json.tool
```

or straight from SQLite:

```bash
sqlite3 data/beyond_the_sale.sqlite3 \
  "select session_id, start, end, text from segments order by session_id, segment_index;"
```

## Tests

```bash
.venv/bin/python tests/test_all.py
```

Prints one line per check and `all checks passed` at the end. Covered:

* chunk ordering and reassembly (out-of-order arrival, duplicate index, holes);
* offline queue retry (IndexedDB persistence, attempt counter, backoff cap,
  upload-once, keeps chunks on a 403) — runs `tests/queue_test.mjs` under Node;
* retention cleanup deletes audio (`0` and `>0`), and never touches a live session;
* no audio download route exists (and audio lives outside `static/`);
* token check rejects bad tokens on `/upload`, `/events`, `/session/start`;
* heartbeat gap detection (pure function + stored `heartbeat_gap`).

The whisper model is not loaded by the tests; the transcription path is exercised
by actually recording something.

## Known limitations

* **The browser can still stop recording.** If the phone kills the tab, the
  browser evicts the page, or iOS suspends the PWA, capture ends. We log it as a gap
  (`recorder_stopped`, `visibility_hidden`, `wake_lock_lost`, `heartbeat_gap`) but
  the missing audio is gone. Wake Lock is not supported everywhere (notably older
  iOS Safari); the screen may lock and the recording may stop.
* Chunk timestamps come from the page, are approximate after pauses and skipped
  silent chunks, and segment times are relative to the reassembled file.
* Skipping silent chunks removes that audio; the skip is recorded as an event.
* WebM chunks are pieces of one stream: only the first carries the container
  header, so chunks are concatenated before transcription. Audio is transcribed
  only after a session finishes.
* One worker thread, CPU only: a long session queues behind the previous one.
  `small` on a laptop is roughly real-time-ish; `tiny` is much faster and less
  accurate.
* A 16 kbps Opus request is a hint; some browsers pick a higher bitrate.
* Auth is a shared token per shop, stored in `localStorage` on the phone. Anyone
  with the phone can record as that shop; do not use this for sensitive data
  without adding real accounts and encryption at rest.
* The tunnel is manual and ephemeral with `--url`; it is not managed by the app.
* `extract_events(transcript)` is a documented stub — event extraction is the next
  step, nothing is implemented yet.
