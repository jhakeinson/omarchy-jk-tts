# Latency baselines

Phase-0 acceptance numbers for the latency plan: what one Kokoro synthesis
costs today, per stage, measured with `tools/bench-startup`. Later phases are
judged against the *spawn → first audio* line; any phase that ships a change
to the hot path records a fresh row here first.

```bash
tools/bench-startup            # default: 2 runs, configured voice, fixed sample text
tools/bench-startup --runs 5 --voice af_heart --text "…"
```

Method notes:

- The bench mirrors `providers/kokoro` line for line (same import order,
  environment, verified-file reads) and adds monotonic probes; audio bytes go
  to /dev/null, so nothing is played.
- Numbers include the sha256 model verification the provider's Phase 1
  baseline ran on every synthesis. That sha256 of the 327 MB model is timed
  with a warm page cache; a superseded, evicted cache would score worse, not
  better.
- "phonemize→first audio" is the gap between the model/pipeline probe and the
  first synthesized byte hitting the pipe — for the sample text it ends once,
  in one chunk, and is the synthesis hot path itself.

## 2026-11-08 · release 1.3.2 baseline (fp32 PyTorch, per-invocation)

Host: local engine, Python 3.12 venv from `lib/engines/kokoro.lock`, voice
`am_michael`, 124-byte sample text, 2 runs, values quoted per stage.

Probes are cumulative offsets from the interpreter engaging the script; the
Δ column converts each probe to that stage's own duration.

| probe (cumulative offset)               | run 1  | run 2  | Δ stage (duration) |
| --------------------------------------- | ------ | ------ | ------------------ |
| sha256 verify config.json               | 0.003 s | 0.003 s | 0.003 s |
| sha256 verify kokoro-v1_0.pth (327 MB)  | 0.170 s | 0.170 s | 0.167 s |
| sha256 verify voices/am_michael.pt      | 0.005 s | 0.005 s | 0.005 s |
| verify total (per press)                | —       | —       | 0.178 s |
| interpreter boot (`python -c pass`)     | 0.012 s | 0.013 s | 0.013 s |
| numpy import                            | 0.042 s | 0.066 s | 0.042 s |
| torch import                            | 0.813 s | 0.833 s | 0.771 s |
| kokoro + misaki import                  | 2.958 s | 2.994 s | 2.145 s |
| KModel load                             | 3.461 s | 3.483 s | 0.503 s |
| KPipeline + voice pack load             | 3.889 s | 3.920 s | 0.428 s |
| phonemize → first audio                 | 5.309 s | 5.340 s | 1.420 s |
| synthesis total                         | 5.309 s | 5.340 s | 1.420 s |
| audio chunks emitted                    | 1       | 1       | — |
| python process lifetime                 | 6.208 s | 6.194 s | — |
| **spawn → first audio**                 | **5.321 s** | **5.353 s** | — |
| spawn → done                            | 5.321 s | 5.353 s | — |
| exit after done (interpreter teardown)  | 0.887 s | 0.841 s | — |

## Phase 1 — verification moved to install time (2026-11-08)

`speak-setup` now digests every manifest file once at install and publishes
`engines/kokoro/.verified` (mode 600): per file, the digest plus a stat
fingerprint (`%d:%i:%s:%.9Y`). `providers/kokoro` compares that fingerprint
per press and runs the full sha256 re-verification (then republishes) only
when the record is missing, stale or unreadable.

Per-press verification cost, measured through `providers/kokoro` with a stub
engine interpreter (`KOKORO_PYTHON=/bin/true`), warm everything:

| path                               | cost    |
| ---------------------------------- | ------- |
| before: sha256 fallback, no record | 0.210 s |
| after: current `.verified` record  | 0.025 s |

The sha256 line in the baseline table remains what the installer pays once;
the bench keeps timing it the old way so the comparison stays on record.

## Phase 2 — kokoro-onnx engine, int8 vs fp32 bring-up (2026-11-08)

The new `kokoro-onnx` engine ships the fp32 Kokoro v1.0 graph
(`kokoro-v1.0.onnx`, 326 MB) with `voices-v1.0.bin`, installed from a pinned
hash-closed lock exactly like the PyTorch engine. The int8 variant the plan
originally pinned was measured on this machine (Ryzen 7 8845HS) during
bring-up and rejected: quantized kernels are ~4.2× slower to synthesize than
fp32, larger than the savings they buy. Numbers both ways:

| probe (engine bring-up, spawn-relative)     | fp32 PyTorch | int8 ONNX | fp32 ONNX |
| ------------------------------------------- | ------------ | --------- | --------- |
| numpy import                                | 0.047 s      | 0.043 s   | 0.043 s   |
| torch / kokoro-onnx import                  | 0.83 s       | 0.078 s   | 0.078 s   |
| kokoro/misaki import (+)                    | 2.96 s       | —         | —         |
| model + voice load                          | 0.93 s       | 0.37 s    | 0.55 s    |
| engine ready (imports + load)               | 3.93 s       | 0.51 s    | 0.67 s    |
| first batch (124-char text)                 | 1.42 s       | 7.11 s    | **1.70 s** |
| **spawn → first audio**                     | 5.32 s       | 7.29 s    | **1.91 s** |

Session-option sweeps (thread counts, optimization levels) did not change the
int8 result; fp32-ONNX retains the PyTorch synthesis speed while dropping the
import/load phase entirely. Provider end-to-end through the full contract
warm: **0.91 s** (`Probe.` sample, stub-free playback path).

## Phase 3 — resident daemon (2026-11-08)

`bin/speak-daemon` keeps one loaded engine per `--engine`, listening on a
private unix socket (`$XDG_RUNTIME_DIR/omarchy-tts/<engine>.sock`, or
`$XDG_STATE_HOME/omarchy-tts/run` without a session runtime dir). The
providers now try that socket first — after the fingerprint verification and
before the inline path, which stays as the complete fallback — so a keypress
only streams one request instead of booting torch.

Per-press end-to-end through the provider (customer fingerprint check, stdin
text, header streaming, PCM bytes relayed, playback discarded), warm page
cache, on this machine:

| press                                        | torch daemon | fp32-ONNX daemon |
| -------------------------------------------- | ------------ | ---------------- |
| first press (daemon loads the model once)     | 4.012 s      | 0.935 s          |
| warm short press (45-char text)               | 0.858 s      | 0.486 s          |
| warm bench press (124-char text)              | 1.636 s      | 1.588 s          |

Daemon side logs confirm the same picture (`first=` is request → first PCM
byte): torch first press 3.967 s, warm short 0.814 s; the framing overhead
per press is ~0.04 s.

Where that leaves the headline number (keypress → first audio, warm):

|                              | inline (1.3.2) | daemon |
| ---------------------------- | -------------- | ------ |
| short keypress, torch engine | 5.32 s         | **0.86 s** |
| bench text, torch engine     | 5.35 s         | 1.64 s |
| short keypress, ONNX engine  | 1.91 s (inline)| **0.49 s** |

The torch daemon's first press is dominated by the one-time model load
(~3.2 s); every press after that in the daemon's 600 s idle window pays only
the request framing and the synthesis itself. The daemon ends itself when the
verified install fingerprint of its loaded engine changes, so a replacement
(reinstall from the Provider tab) can never keep it serving stale files; the
next press brings a fresh daemon up, or falls back inline.

## Phase 4 — synthesis cache (2026-11-08)

`lib/cache.sh` keys (engine, voice, speed, exact text bytes) and stores the
resulting PCM in `~/.cache/omarchy-tts/audio/` (owner-only), refresh-on-hit
LRU at 256 MB / 30 days. Audio is staged during synthesis and renamed into
place only after a clean press, so nothing partial is ever cached; `speak
--verify` runs with the cache off so health checks keep exercising real
synthesis.

Measured end to end through providers/kokoro, press-to-press (playback
discarded, warm page cache), bench text "The quick brown fox jumps over the
lazy dog.":

| same text repeated                           | keypress → first audio |
| -------------------------------------------- | ---------------------- |
| first press (daemon synthesizes: cold model)  | 5.07 s                 |
| repeat (cache hit, daemon dead or alive)      | **0.04 s**             |

The cache hit path touches no engine: the press is a file read plus player
startup, which also means a repeated press keeps working when an engine is
being replaced or has never been installed at all. New text always
synthesizes; repeated text is served until the entry ages or is evicted.

Full headline so far (keypress → first audio, same machine):

|                                | 1.3.2 inline | daemon | cached repeat |
| ------------------------------ | ------------ | ------ | ------------- |
| short keypress, torch engine   | 5.32 s       | 0.86 s | **0.04 s**    |
| bench text, torch engine        | 5.35 s      | 1.64 s | 0.04 s        |
| short keypress, ONNX engine    | 1.91 s       | 0.49 s | 0.04 s        |
