# Plan: Streaming TTS for voice calls (sentence-level pipelining)

Status: **deferred** — spike done, not worth building yet (see Findings).

## Findings (2026-06-07)

A logging-only `render_message_event` override was added to the adapter and
exercised on live calls. **Result: zero stream deltas fired.** Root cause:
Hermes `StreamingConfig.enabled` defaults to `False` (gateway/config.py), so
the stream consumer is never created and `render_message_event` is never called.

Combined with switching calls to a faster model (`ministral-14b`), per-turn AI
latency dropped to ~1.5–4.6s and a full turn to ~5–8s. The remaining benefit of
streaming (overlapping ~2-4s AI with ~2s TTS) is now small, while the cost is
real: enabling `streaming.enabled` is **global** (text chats get progressive
message edits) and the stream consumer performs its own chat delivery that would
conflict with the call `send()` intercept.

**Decision:** defer. Revisit only if longer call responses become common and the
latency is felt. To revisit: set `streaming.enabled: true` in config.yaml, re-add
the `render_message_event` spike (removed after this finding; see git history of
adapter.py around the voice branch), confirm deltas fire for call chats, then
proceed with the design below.

---

## Goal

Cut perceived call latency by speaking the AI's reply **as it is generated**
instead of waiting for the full response. Today the pipeline is strictly
sequential per turn:

```
STT ──▶ AI (full response) ──▶ TTS (full response) ──▶ play
```

With sentence-level streaming, AI generation, TTS, and playback overlap:

```
STT ──▶ AI streaming ──┬─ sentence 1 ─▶ TTS ─▶ play ────────────
                       ├─ sentence 2 ─────────▶ TTS ─▶ play ─────
                       └─ sentence 3 ──────────────────▶ TTS ─▶ play
```

The bot starts talking after the **first sentence** is generated + its TTS
(~2-3s) instead of after the whole response + whole TTS (~10-18s). For a 3-4
sentence answer this roughly halves time-to-first-audio and removes most of the
dead air.

## Background: the streaming hook

Hermes streams assistant output as structured events. The adapter can observe
them by overriding `BasePlatformAdapter.render_message_event(event, sink)`
(gateway/platforms/base.py ~line 1955), which receives:

- `MessageChunk(text=...)` — incremental text deltas
- `MessageStop(final=bool)` — segment / terminal boundary
- `Commentary(text=...)` — side commentary

The contract is presentation-only: nothing rendered here is persisted; history
is owned by the agent. This is exactly the seam we need — accumulate
`MessageChunk.text` during an active call and drive TTS from it.

## Design

### 1. Detect "this stream belongs to an active call"

`render_message_event` has no chat_id argument, so we need to know the current
streaming session maps to a call. Options to investigate (in order of
preference):

- The `sink` (GatewayStreamConsumer) likely exposes the session/source — read
  chat_id from it and check `CallManager.has_active_call(chat_id)`.
- Failing that, track "currently-streaming call" state: since calls are
  effectively single-turn-at-a-time, set a `_streaming_call_chat` on the adapter
  when a call utterance is injected and clear it on response completion.

**Verification needed:** confirm `render_message_event` is actually invoked for
our injected call messages (streaming must be enabled for the session/platform).
If it is not called, fall back plan: hook the stream consumer differently or
keep the whole-response path. This is the #1 risk and must be validated first
with a logging-only spike.

### 2. Sentence segmentation

Accumulate deltas into a buffer. Emit a chunk to TTS when:
- a sentence terminator (`.`, `!`, `?`, `…`, newline) is seen, AND
- the accumulated chunk is at least N chars (e.g. 25) to avoid choppy
  micro-utterances ("Yes." "OK.").

Flush any remainder on `MessageStop(final=True)`. Strip markdown/emoji/URLs the
same way `prepare_tts_text` does (reuse `BasePlatformAdapter.prepare_tts_text`).

### 3. Ordered TTS + playback

Sentences must be spoken in order. `HermesAudioTrack._queue` already plays in
FIFO order, but TTS runs in worker threads and can finish out of order. Use a
per-call **asyncio task chain** (each TTS awaits the previous one's enqueue) or
an ordered worker queue inside `CallManager`:

```
_tts_queue: asyncio.Queue[str]          # sentences
_tts_worker: single task per call       # pulls, TTS, enqueue_tts_frames — serial
```

This keeps decode/enqueue serialized and in order while AI keeps generating.

### 4. Suppress the final whole-response send()

Today `adapter.send()` intercepts the final response and speaks the whole thing.
With streaming we already spoke it sentence-by-sentence, so the final `send()`
must NOT speak it again. Add a per-call flag: if the streaming path emitted any
audio for the current turn, `send()` skips `play_response` (just returns
success). Reset the flag at the start of each injected utterance.

Edge case: if streaming produced nothing (hook not called, or empty), fall back
to the existing whole-response `play_response` so we never go silent.

## Files to touch

| File | Change |
|---|---|
| `adapter.py` | Override `render_message_event` → forward call-related chunks to `CallManager`; set/clear "streaming turn" flag; make `send()` skip when streaming already spoke |
| `call_handler.py` | `CallManager.feed_stream_text(chat_id, text)` + `flush_stream(chat_id)`; sentence segmentation; per-call ordered TTS worker; `HermesAudioTrack` unchanged |

## Verification

1. **Spike first:** add logging-only `render_message_event` override, make a
   call, confirm `MessageChunk` deltas arrive with the call's chat_id. If not,
   stop and rethink the hook — do not build the rest until this is confirmed.
2. Measure time-to-first-audio before/after (extend the existing `perf` logs
   with a `first_audio` timestamp relative to utterance end).
3. Confirm no double-speaking (final send suppressed) and no dropped tail
   (last sentence always flushed on MessageStop final).
4. Confirm ordering across 3+ sentence responses.
5. Confirm fallback path still works when streaming is unavailable.

## Notes / tradeoffs

- The concise-reply `channel_prompt` (1-2 sentences) reduces the benefit —
  streaming matters most for longer answers. Consider relaxing the brevity
  prompt if streaming lands, so answers can be richer without latency cost.
- Interruption/barge-in (user talks while bot is speaking) is out of scope here
  but becomes more relevant with longer streamed replies — possible follow-up:
  flush the outgoing queue when a new utterance is detected.
