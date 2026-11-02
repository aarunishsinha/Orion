# Benchmarks

Two measurements of Orion, both run on 2026-11-02:

1. **Voice-to-response latency**: the time from when the user releases the push-to-talk key until Orion starts speaking.
2. **Global quota across replicas**: whether the Google Calendar rate limit holds when several workers run at once, and whether the workers actually share the load.

| Script | Purpose |
|---|---|
| [`summarize_latency.py`](summarize_latency.py) | Summarises per-turn timings that the orchestrator writes to `logs/latency.jsonl` |
| [`quota_burst.py`](quota_burst.py) | Sends calendar jobs to the arq workers and checks the rate limit and how jobs were spread across replicas |

---

## Environment

| | |
|---|---|
| Machine | MacBook Pro, Intel Core i9-9880H @ 2.30 GHz (8 cores / 16 threads), 16 GB RAM |
| OS | macOS 26.6.2 (25G83) |
| Speech-to-text | `whisper-cli` from Homebrew whisper-cpp 1.8.4 (ggml 0.9.11, BLAS backend), model `ggml-base.en`, run with `-ng` (CPU only) |
| LLM | Ollama 0.22.1, model `qwen2.5:3b` (default tag), `num_predict=256`, `temperature=0.7`, not streamed. CPU only: Ollama has no GPU backend on Intel Macs. |
| TTS | macOS `say`, voice `Ava (Premium)`, 190 words per minute; audio is rendered to a file, then played with `afplay` |
| Orchestrator | Python 3.11.15 (`client_hud` uv environment) |
| Calendar stack | Docker 29.2.1, Compose v5.0.2. Redis 7.4.12 (`redis:7-alpine`), arq 0.27.0, Python 3.12 containers |

---

## 1. Voice-to-response latency

### Why we measured it

What a user notices about a voice assistant is the silence between finishing speaking and hearing the reply. Orion handles each request in three steps, one after another (speech-to-text, then the LLM, then speech synthesis). Timing each step separately shows which one to optimise first.

- We report p50 for the typical case and p90 for the slow tail.
- The cold first call is reported separately. It includes loading the model and doesn't reflect normal use.

The LLM's tokens/sec (taken from Ollama's `eval_count` / `eval_duration`) separates how fast the model runs on this CPU from how long its replies are.

### What was timed

Running the orchestrator with `ORION_BENCH=1` turns on timing. Each push-to-talk turn is timed with `time.perf_counter()` around three calls in [`orchestrator_v3.py`](../orchestrator/orchestrator_v3.py):

| Stage | Call | Covers |
|---|---|---|
| `stt` | `audio_engine.stop_recording_and_transcribe()` | Closing the microphone stream, writing the WAV file, running `whisper-cli` and parsing its output |
| `llm` | `query_llm()` | The HTTP request to Ollama's `/api/chat`; Ollama's own timing breakdown is saved with each turn |
| `tts` | `tts_engine.speak()` | `say -o` rendering the whole reply, `afinfo` reading its duration, and starting `afplay`. `speak()` returns once playback has started, so this stage ends at the first sound. |
| `total` | from the start of `stt` to the return of `speak()` | Everything above plus the small gaps between stages (measured as effectively zero) |

Bench mode also does the following:
- **Clears conversation history before every turn.** Without this, each prompt is longer than the last and later runs are slower. With it, every run sends the same system prompt plus one user message (about 90–130 prompt tokens).
- **Records the utterance length (`audio_s`)** so that runs can be filtered by how long the user spoke.

The orchestrator previously had a fixed 2-second `time.sleep` that kept the transcript on screen before the LLM call. It was removed before these measurements, and the transcript now stays visible while the LLM runs.

### Procedure

1. Start Ollama fresh with `ollama serve`, so the first call includes loading the model.
2. Start the orchestrator with `ORION_BENCH=1 ./run.sh`.
3. Do 27 push-to-talk turns with real speech into the laptop microphone. Turn 1 is the cold run.
4. Run `python3 bench/summarize_latency.py`.

The plan called for 20 warm runs with 3–8 s utterances. Of the 26 warm runs, 10 were longer than 8 s (up to 17.3 s). The headline figures below use the 16 runs between 3 and 8 s. `summarize_latency.py` reports every warm run, so the filtered figures were computed separately from `logs/latency.jsonl` (keeping turns with `3 <= audio_s <= 8`). That file is git-ignored and not committed.

### Results

**Warm runs, 3–8 s utterances (n = 16)**

| Stage | p50 | p90 |
|---|---|---|
| Speech-to-text | 2.91 s | 3.26 s |
| LLM | 4.21 s | 5.76 s |
| TTS until playback starts | 2.25 s | 2.47 s |
| **Total** | **9.33 s** | **11.48 s** |

**LLM speed:**
- Generating the reply: 7.6 tok/s at p50, 8.4 at p90.
- Reading the prompt: about 59 tok/s.
- Replies averaged about 20 tokens.

<details>
<summary>All 26 warm runs, without the length filter (output of <code>summarize_latency.py</code>)</summary>

| Stage | p50 | p90 |
|---|---|---|
| Speech-to-text | 3.01 s | 4.02 s |
| LLM | 4.57 s | 7.03 s |
| TTS until playback starts | 2.28 s | 2.51 s |
| Total | 9.68 s | 13.36 s |

LLM output: p50 7.5 tok/s, p90 8.5 tok/s.
</details>

**Cold first turn (7.7 s utterance):** 23.09 s in total.

| Stage | Time | Notes |
|---|---|---|
| Speech-to-text | 2.79 s | |
| LLM | 16.50 s | 3.51 s loading the model, then 10.11 s reading a 90-token prompt (about 1.5 s when warm) |
| TTS until playback starts | 3.80 s | |

### What the results tell us

- **Speech-to-text has a fixed cost of about 2.5 s.** A 3.6 s utterance took 2.7 s to transcribe and a 17.3 s one took 6.1 s, so most of the time doesn't depend on how long the user spoke. Every call starts a new `whisper-cli` process, which loads the model again. A whisper process that stays running would remove most of that cost.
- **LLM time is about 1.5–2 s reading the prompt plus about 2.7 s generating the reply.** At about 8 tok/s, every 10 extra tokens in the reply add about 1.3 s. Because the request isn't streamed, the user hears nothing until the whole reply is generated. Network and HTTP time is negligible.
- **TTS has a fixed cost of about 1.7 s before any sound plays.** A 7-character reply still took 1.7 s, because `say -o` renders the entire reply with the Premium voice before `afplay` starts. Streaming speech, or speaking each sentence as the LLM produces it, would cut this.
- **The total is just the three stages added up.** None of the coordinating code between stages adds measurable time.
- **The first call costs about 14 s extra,** mostly a slow first read of the prompt rather than model loading. Warming the model with a short request at startup would hide this from the user.

---

## 2. Global quota across replicas

### Why we measured it

The calendar MCP server doesn't call Google directly. It puts each tool call on a Redis queue, and stateless arq workers pick the calls up and run them. Two claims depend on this design:

- **Horizontally scalable:** adding worker replicas adds capacity, and jobs actually spread across the replicas.
- **Global quota:** the 10-requests-per-10-seconds limit applies to all replicas together, not to each one. The limiter in [`rate_limiter.py`](../servers/google_calendar/src/rate_limiter.py) keeps a shared sliding window in Redis and checks it in one atomic step. If each worker counted on its own, 3 replicas would let up to 30 requests through.

### Rate-limiter fix made before testing

The original limiter recorded every request in the window, **including rejected ones**. That had two effects:
- Under steady load the window never emptied, so fewer than 10 requests were admitted per window.
- Sending 50 jobs at once could only ever show one window.

The limiter now runs a single Redis script that removes expired entries, counts what's left, and records the request only if it's admitted. It also uses millisecond timestamps instead of whole seconds.

Rejected jobs fail; they are not retried. Retrying with a delay was considered and rejected: the MCP server stops waiting for a job after 5 seconds, so a delayed `create_event` could still run after the caller had been told it timed out.

### Setup

```bash
# from the repo root
docker compose up -d --build --scale worker=3     # redis, mcp_server, 3 × worker
uv run --project servers/google_calendar python bench/quota_burst.py            # random spacing, ~2/s
uv run --project servers/google_calendar python bench/quota_burst.py --rate 0   # all at once
docker compose down
```

- Redis is mapped to host port **6380**, which avoids clashing with other local Redis instances on 6379. The containers reach it over the Compose network.
- `quota_burst.py` connects to `redis://localhost:6380/0` and sends `list_upcoming_events` jobs **straight to arq**, bypassing the MCP HTTP layer. Before each run it deletes only the `rate_limit:mcp_worker` key.
- Each job's outcome is read from its arq result. Admission times and the replica that ran each job come from the workers' `docker compose logs`. Each job logs exactly one `Rate limit admitted` or `Rate limit exceeded` line, with millisecond timestamps.
- **No Google credentials were configured.** Admitted jobs therefore fail one step later, on the missing `credentials.json`. "Admitted" means the job passed the rate limiter, not that a Google API call succeeded.
- The script checks two things:
  - **admitted per fixed 10 s window,** counted from the first admission;
  - **the most admitted in any rolling 10 s span.** A sliding-window limiter only guarantees this second number, which must never exceed 10.

### Results

| Run | Admitted / rejected | Admitted per 10 s window | Max in any rolling 10 s | Jobs per replica |
|---|---|---|---|---|
| 50 jobs, random spacing averaging 2/s | 22 / 28 | 10, 10, 2 | **10** ✅ | worker-1: 19, worker-2: 19, worker-3: 12 |
| 50 jobs at once | 10 / 40 | 10 | **10** ✅ | worker-1: 28, worker-3: 22 |
| *(first attempt)* 50 jobs exactly every 0.5 s | 30 / 20 | 10, 10, 10 | **10** ✅ | worker-1: 50 |

The third window in the first row is partial: the jobs stopped arriving about 2 s into it.

### What the results tell us

- **The quota is global.** In every run, at most 10 jobs were admitted in any 10-second span, even though up to 3 workers were checking the limit at the same time. The check-and-record step runs as one Redis operation, so replicas can't race past the limit.
- **Admitted throughput matches the limit exactly** (10 per window) whenever demand exceeds it. This confirms the fix: rejected requests no longer use up quota.
- **Jobs spread across replicas when arrivals are irregular.** With random spacing, all 3 workers processed jobs (19 / 19 / 12).
- **arq doesn't share jobs out evenly.** Each worker checks the queue every 0.5 s, and whichever checks first takes the job.
  - In the first attempt, jobs arrived exactly every 0.5 s, in step with those checks, so worker-1 took all 50.
  - With all 50 jobs at once, rejected jobs finished in milliseconds, so two workers cleared the queue before the third checked.
  - Under light load, extra replicas add headroom rather than evenly shared work. They take a real share once a single worker can't keep up.
- **Not measured:** how much the queue adds to tool-call latency compared with calling Google directly. That needs working Google credentials.
