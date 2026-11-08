# Architecture and review guide

The plugin has a six-tab settings panel: Provider, Voice, Text, Screen,
Languages, and Keys. Its command-line boundary remains independently useful
and testable.

## Ownership boundaries

- `bin/speak` is the public speech and settings API.
- `lib/config.sh` owns schema migration, validation and atomic config writes.
- `lib/health.sh` owns shared, atomic health-cache invalidation.
- `lib/cache.sh` owns the synthesis result cache: keying, staged commits,
  LRU/TTL eviction and refusal of unsafe cache locations.
- `lib/sanitize.py` is the one sanitizer used for speech and live preview.
- `bin/speak-daemon` owns resident synthesis for the heavy local engines:
  request validation, peer credentials, per-request engine fingerprint
  checks and the backend implementations (torch, fp32 ONNX).
- provider executables declare small metadata headers consumed by `--info`.
- `bin/speak-voice` owns Piper catalogue/download lifecycle.
- `bin/speak-bindings` owns only the text between its markers in
  `~/.config/hypr/bindings.lua`; it backs up and validates every write. Its
  private JSON stores only chord choices—labels, commands, and installation
  paths are always derived from the current plugin.
- `components/TtsController.qml` is the async bridge from QML to those CLIs.
- `Panel.qml` owns presentation and transient interaction state.

QML uses argv arrays for user-controlled values. Shell interpretation is
limited to a fixed allowlist handled by the graphical setup backend.
Cloud keys are entered in a masked panel field and stored with Secret Service;
the key never returns to QML or appears in a process argument.

## Configuration

`config.json` schema version 4 is additive and backwards compatible. Startup
fills missing defaults without replacing existing values. ElevenLabs has no
global default voice: the first successful account-voice refresh selects one
only when the user has not already made a choice. Schema 3 migrates the former
ElevenLabs default model to `eleven_flash_v2_5`; schema 4 standardizes Gemini
on the Google AI Developer API. Invalid JSON, oversized files, and unsafe
nested shapes are preserved with an `.invalid.<timestamp>` suffix before
type-safe defaults are restored. Writes are limited to an allowlist, serialized,
and use an atomic rename with mode 0600. Existing config symlinks keep pointing
to the same target, while dangling symlinks are rejected.

Runtime state is stored in a private, user-owned directory. Speech and
background jobs record both process ownership and identity; callers must only
clear state they still own, and the UI does not expose cancellation until that
identity is confirmed. Provider/OCR health entries carry fingerprints of the
adapter, relevant configuration, and credential source, so old proof becomes
untested after meaningful changes. QML settings writes are serialized, with
redundant pending writes to the same property coalesced.

Provider headers are the capability boundary consumed by `speak --info`.
Alongside descriptive fields, adapters can declare byte/character limits,
credential names and environment variables, deprecation, account voice
refresh, and usage refresh. The panel acts on those normalized fields instead
of inferring behavior from a filename.

Speech preparation and playback have separate, identity-checked ownership
records. A new utterance atomically hands off under the speech lock; Stop can
cancel selection, capture, OCR, provider preparation, or playback without PID
reuse races. Public text and capture inputs, catalogue records, downloads, and
cloud responses all have explicit size ceilings.

Managed engine setup and Piper voice publication are transactions. Engine
activation records a private recovery journal before replacing a proven
generation; voice repair stages and verifies both the model and sidecar before
publishing the pair under the same lock Piper holds while reading them. A later
invocation repairs artifacts left by abrupt process death or power loss.

### Resident daemon

`bin/speak-daemon` is a Python stdlib process per heavy engine, started on
demand by `omarchy-tts-<engine>.socket` (a systemd user socket unit; the
daemon inherits the listener as fd 3) and parked after `--idle-exit` (600 s)
of silence. The win: torch's ~3.2 s model load is paid once per idle window
instead of once per press. Protocol: one JSON request line
`{voice, speed, text}`, then raw s16le PCM at 24 kHz until the daemon closes
the connection — the provider contract shared with inline synthesis, so the
daemon is invisible above the provider layer.

Safety posture: `SO_PEERCRED` admits only the owner; requests are bounded
(1 MiB header, 200k characters); a symlinked socket path is refused; the
engine install fingerprint loaded into the daemon is re-checked with a `stat`
every request and any change ends the daemon so the next press starts against
freshly verified files. Providers reach it only after their own full replay
of the verification rung, and always fall back to inline synthesis if the
daemon refuses the request before the first audio byte. A hand-supplied
interpreter (`KOKORO_PYTHON`/`KOKORO_ONNX_PYTHON`) skips the daemon by
design: those are verify/experiment runs, not release-managed engines.

### Synthesis cache

`lib/cache.sh` (sourced by the local providers) stores committed presses in
`~/.cache/omarchy-tts/audio/` keyed `sha256(provider ∥ voice ∥ rate ∥ text
bytes)`. Keyed presses read stdin into a private temp file first so the key,
the daemon request and the inline synthesis all see identical bytes. Audio
stages under `.inflight-<key>.part` while synthesizing and is renamed in
place only when its whole pipeline exits 0 (`mv` is atomic), so a cancelled
or failed press never caches partial audio; an empty stage is refused too.
The directory is 0700, entries 0600, symlinked directories and entries
refused. Hits refresh mtime (that is the LRU order and the freshness clock),
and `audio_cache_trim` drops expired entries then the oldest past
`TTS_CACHE_LIMIT_MB` (256 MB) / `TTS_CACHE_TTL_DAYS` (30). `TTS_CACHE=0`
disables it; `speak --verify` sets it and `TTS_DAEMON=0` together so health
checks always exercise real synthesis. Cloud providers are never cached:
their output can change server-side under the same voice id.

## Deliberate design changes

- Screen remains a dedicated tab, with recognition languages separated into
  their own focused browser.
- Keys use Omarchy's current Lua configuration, not the obsolete `.conf` file
  in the original mockup.
- Provider installs use reviewed Omarchy/uv commands and require confirmation.
- Direct engine packages are release-pinned; Kokoro deliberately uses an
  isolated Python 3.12 because its published metadata excludes newer Python.
- API keys use the system keyring rather than a plaintext keys file.
- Live speed changes apply to the next utterance because current providers do
  not expose safe in-place playback speed updates.

## Suggested reviewer path

1. `lib/config.sh` and `bin/speak --info/--set`
2. `components/TtsController.qml`
3. the six sections in `Panel.qml`
4. `bin/speak-setup` transaction and recovery behavior
5. `bin/speak-voice` catalogue, verification, publication, and recovery
6. `bin/speak-bindings` safety and coupled rollback behavior
7. provider/OCR request construction, bounds, and error normalization

Run `python3 -m unittest discover -s tests -v` and the ShellCheck command from
the README. The suite includes static QML interaction contracts; a full visual
review still requires omarchy-shell.

## Re-pinning Piper voices

Voices download from one immutable revision of `rhasspy/piper-voices`, and
every model and sidecar is verified against `lib/piper-voices.sha256`. To move
to a newer upstream revision:

```bash
tools/pin-piper-voices <40-hex revision>
```

It rewrites `lib/piper-voices.json`, `lib/piper-voices.sha256`, and the
`PIPER_VOICES_REV` constant in `bin/speak-voice`. Review the diff like any
other dependency bump; the digests are the trust root for every download.

## Re-locking engine dependencies and models

```bash
tools/pin-engines lock      # rebuild lib/engines/*.lock from *.in (uv, hashed, wheel-only)
tools/pin-engines models    # re-fetch every pinned model artefact and confirm its digest
```

The locks are complete closures for Linux x86_64 / Python 3.12, compiled
against the index as it stood at `lib/engines/EXCLUDE_NEWER`; move that
instant forward to pick up newer releases, then re-run `lock`. CI fails if the
locks do not regenerate byte-for-byte or if any requirement lacks a wheel.

A dependency with no published wheel (today only `docopt`, via
kokoro → misaki → num2words) is served from the reviewed flat index
`lib/engines/wheels`. Its wheel is built reproducibly from the hash-verified
sdist so a reviewer can recreate it bit-for-bit:

```bash
curl -sSLO https://files.pythonhosted.org/packages/a2/55/8f8cab2afd404cf578136ef2cc5dfb50baa1761b68c9da1fb1e4eed343c9/docopt-0.6.2.tar.gz
echo '49b3a825280bd66b3aa83585ef59c4a8c82f2c8a522dbe754a8bc8d08c85c491  docopt-0.6.2.tar.gz' | sha256sum -c
tar xzf docopt-0.6.2.tar.gz && cd docopt-0.6.2
SOURCE_DATE_EPOCH=0 uv build --wheel --out-dir ../dist .
sha256sum ../dist/docopt-0.6.2-py2.py3-none-any.whl   # must match lib/engines/wheels/SHA256SUMS
```

The shipped wheel was produced by uv 0.12.8 with setuptools 84.0.0 as the
build backend (recorded in its `WHEEL` metadata) under `umask 022`. If a
later backend changes the metadata and the hash no longer matches, compare
the payload instead: `docopt.py` and `LICENSE-MIT` inside the wheel must be
byte-identical to the files in the verified sdist. Locks are maintained with
the uv release in `lib/engines/UV_VERSION`; `tools/pin-engines lock` refuses
any other.

To bump a model, edit `lib/engines/models.json` by hand with the new immutable
source and digest, then run `models` to prove it. A digest that no longer
matches is the event the manifest exists to catch, so the tool never rewrites
one.
