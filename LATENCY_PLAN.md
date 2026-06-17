# Latency Reduction Plan

Goal: cut the ~3+ second delay between the end of a user's speech and Jack's spoken
response down to roughly one second.

## Current pipeline

The pipeline is fully sequential with no overlap between stages:

```
end of speech (pause_threshold 0.3s)
  → FLAC conversion on the Pi
  → HTTPS POST to Cloud Run (new TLS connection each time)
  → STT + LLM in the cloud function (returns only the COMPLETE text)
  → HTTPS POST to ElevenLabs (new TLS connection each time)
  → ffplay spawn + stream probing
  → audio playback begins
```

End-of-speech detection is already tight (`pause_threshold: 0.3`), so all of the
delay is downstream of the microphone.

## Plan, in order of expected payoff

### 1. Eliminate Cloud Run cold starts

If the function uses the default `min-instances=0`, the first request after a quiet
period pays a multi-second cold start — and trick-or-treater traffic is exactly the
bursty pattern that triggers it.

- Diagnose: compare back-to-back interactions in the Pi logs. Slow first response
  followed by fast subsequent ones = cold starts.
- Fix: `gcloud run services update jack-o-lantern-function --min-instances=1`
  (≈ a few dollars/month for the idle instance; could be enabled only for October).

### 2. Overlap LLM generation with TTS

Today the Pi waits for the entire LLM response before sending the first byte to
ElevenLabs. If the LLM takes 1.5s to generate the full reply, all of it is dead time.

- Change the cloud function to stream its response (chunked HTTP).
- On the Pi, send the first complete sentence to ElevenLabs as soon as it arrives,
  while the rest is still generating.
- ElevenLabs' WebSocket input-streaming API is purpose-built for this and is the
  preferred transport once text arrives incrementally.

Touches both `google_interface.py` (Pi side) and the cloud function.

### 3. Instruct the LLM to be brief

Shorter responses generate faster and play faster, and one-to-two-sentence quips fit
the character better than monologues. One-line prompt change in the cloud function;
compounds with item 2.

### 4. Cut ffplay startup buffering

ffplay probes the stream format before playing, adding noticeable delay on a Pi.

- Quick fix in `index.py` (`elevenlabs_stream`):

  ```python
  ffplay_cmd = ["ffplay", "-nodisp", "-autoexit",
                "-probesize", "32", "-analyzeduration", "0",
                "-fflags", "nobuffer", "-f", "mp3", "-"]
  ```

- Better: request raw PCM from ElevenLabs (`output_format=pcm_22050` query param)
  and pipe straight to `aplay -r 22050 -f S16_LE -c 1` — no decoder probing at all,
  and aplay starts faster than ffplay.

### 5. Reuse TLS connections

Both `google_interface.py` and `elevenlabs_stream` open a fresh HTTPS connection per
request — a TLS handshake (~150–300 ms) each, up to ~half a second combined per
interaction.

- Create a module-level `requests.Session()` in each file and use `session.post(...)`.
- Optionally pre-warm the connections at startup.

### 6. Measure each stage

The log format already includes millisecond timestamps. Add a `logger.info` at each
stage boundary (FLAC done, cloud responded, TTS first byte, playback started) and run
for an evening to confirm which stage is the real bottleneck before optimizing further.

## Future option: realtime speech-to-speech

A realtime API (e.g. Gemini Live, staying within Google Cloud) collapses STT + LLM +
TTS into one bidirectional audio stream and reaches sub-second latency naturally.
This replaces most of the current pipeline, so it's a rewrite rather than a tuning
step — items 1–5 should reach ~1 second without it.
