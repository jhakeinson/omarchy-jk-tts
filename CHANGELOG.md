# Changelog

All notable changes are documented here. This project follows Semantic
Versioning.

## Unreleased

## 1.4.1 - 2026-11-08

- Fixed the panel rendering almost unreadable on themed machines: bare `Color`
  references in plugin QML resolve to QtQuick's built-in color helper object
  (`QQuickColor`) for files outside the shell's config folder, so every theme
  lookup (`Color.accent`, `Color.popups.*`, `Color.muted`, …) evaluated to
  undefined and QML drew failed color bindings as black text on the dark
  panel. All QML files now import the shell's `qs.Commons` singleton through
  a qualified alias (`Commons.Color.*`), the same pattern Omarchy's own
  third-party plugins use; verified against Catppuccin Mocha that headers,
  tabs, accents and body text now match the theme exactly.
- Fixed the kokoro-onnx install flow dying with `curl: (23)` (the models
  directory was never created before curl staged its download).
- Published as the standalone `jhakeinson/omarchy-jk-tts` repository, with
  install and CI badge links pointed there. The plugin id
  (`io.github.hikari112.tts`) is unchanged so existing installs keep their
  configuration, engine directories and systemd unit names.

## 1.4.0 - 2026-11-08

- Cut the keypress → first-audio latency of the heavy local engines by more
  than a hundredfold for repeated phrases, in two coordinated layers:
  - A resident synthesis daemon (`bin/speak-daemon`) keeps one engine process
    per heavy engine with the model loaded across key presses, listening on a
    private per-user unix socket started on demand by a systemd user socket
    unit. Providers try the daemon first, after full fingerprint
    verification, and fall back to inline synthesis before the first audio
    byte on any refusal, so the daemon is an optimization and never a
    dependency. Warm key presses drop from ~5.3 s inline to ~0.86 s (torch)
    and ~0.49 s (ONNX fp32) end to end.
  - A synthesis result cache (`lib/cache.sh`) keyed on (engine, voice,
    speed, exact text bytes) stores committed presses under
    `~/.cache/omarchy-tts/audio/` (owner-only, 0600) with refresh-on-hit LRU
    (256 MB, 30 days) and renames audio into place only after a clean press,
    so cancelled or failed presses are never cached; repeated phrases play
    from disk in ~0.04 s. `TTS_CACHE=0` opts out; `TTS_CACHE_LIMIT_MB` and
    `TTS_CACHE_TTL_DAYS` bound it; `speak --verify` bypasses it so health
    checks keep proving real synthesis.
- Add an fp32 ONNX Kokoro engine (`kokoro-onnx`, pinned `kokoro-v1.0.onnx` +
  `voices-v1.0.bin`) installed from a hash-pinned lock exactly like the
  PyTorch engine. The int8 variant the plan originally pinned was measured
  during bring-up and rejected: quantized kernels synthesized ~4.2× slower
  than fp32 (7.11 s vs 1.70 s first batch), larger than the startup they
  save; the fp32 ONNX engine keeps torch's synthesis speed while dropping
  the torch import and model-load phase entirely.
- Publish the verification record each press builds for a heavy engine
  (digest + file fingerprint of every verified model file) under
  `engines/<engine>/.verified` (owner-only, atomic), so a press whose record
  is current pays two `stat` calls instead of full model digests. Any drift
  in the fingerprint triggers a cheap sha256 re-check and, on a pass, a
  republished record.
- Extend `speak --info` with a `latency` object: per-engine daemon socket
  liveness plus the cache's enabled state and store size, so the settings
  panel can show what each optimization is actually doing.

## 1.3.2 - 2026-09-06

- Resolve `pkexec` and `pacman` at fixed canonical paths and admit them across
  the privilege boundary only when the executable and every ancestor directory
  are root-owned, not group- or world-writable, and free of symlink components,
  re-checked immediately before each privileged call; nothing from `PATH` is
  ever handed to `pkexec`.
- Install every managed engine from a complete, hash-pinned, wheel-only
  dependency closure shipped in the plugin (`lib/engines/*.lock`, reproducible
  from `*.in` at the instant in `lib/engines/EXCLUDE_NEWER`) with `uv pip sync
  --require-hashes --only-binary :all:`, so no resolver result, index change or
  source build can add code this release did not review. The one dependency
  that publishes no wheel (`docopt`) ships as a reviewed, reproducibly built
  wheel in `lib/engines/wheels`, verified before `uv` runs. The spaCy model
  wheel is part of the Kokoro lock.
- Pin the EasyOCR and Kokoro model artefacts to fixed sources with SHA-256
  digests shipped in `lib/engines/models.json`; the installer fetches and
  verifies them itself (library downloaders are never enabled), and each engine
  verifies the files again immediately before loading them. Kokoro runs with
  the Hugging Face hub client offline. Piper voices from the shipped catalogue
  are verified before load in the same way.
- Build engine environments relocatable, so console scripts survive the
  installer's staging rename (Piper failed its proof with exit 126).
- CI applies every lock in hash-enforcing, wheel-only mode and checks that the
  locks regenerate byte-for-byte. `tools/pin-engines` maintains them.
- **Upgrading:** every engine environment built by an earlier release shows as
  not installed, because it was not built from this release's lock; reinstall
  Piper, Kokoro or EasyOCR once from the panel. Piper voices are unaffected.

## 1.3.1 - 2026-09-04

- Pin Piper voice downloads to one immutable revision of `rhasspy/piper-voices`
  and verify every model and sidecar against SHA-256 digests shipped in the
  plugin (`lib/piper-voices.sha256`); the catalogue ships too. A file without a
  shipped digest is refused, MD5 is no longer used, and a change to the
  upstream branch can no longer alter what is downloaded or trusted.
  `tools/pin-piper-voices` regenerates the pinned data for a new revision.

## 1.3.0 - 2026-09-03

- Add selectable EasyOCR, OpenAI Vision, and Google Cloud Vision recognition
  engines, with per-engine language state, explicit language discovery, and
  safe Tesseract language-pack installation.
- Add Gemini and Google Cloud Text-to-Speech providers, account-backed Google
  and ElevenLabs voice browsing, and metadata-driven refresh/usage actions.
- Expand the settings panel to six scrollable views, add filtered cloud voice
  browsing, make inline controls keyboard-safe, and clearly distinguish local
  processing, cloud disclosure, key sources, and environment-key remediation.
- Remove the `espeak-ng` and Speech Dispatcher providers; a configuration that
  still names one is moved to Piper on first run.
- Bind readiness to the exact adapter, relevant configuration, and credential
  source so stale health results cannot survive an upgrade, voice/language
  change, or key mutation; bound provider probes that do not return.
- Close setup and voice-download cancellation races by withholding cancellable
  state until the worker publishes a verified PID identity, and reconcile jobs
  that terminate unexpectedly.
- Accept fully non-Latin selections, keep sanitizer output within its exact
  configured limit, and enforce Google Cloud's 5,000-byte UTF-8 request limit
  before sending text.
- Normalize embedded Google Vision failures without echoing remote response
  details, record HTTP-200 error envelopes accurately, and reject invalid
  Gemini API-host settings before any network request.
- Pin EasyOCR and the Kokoro spaCy model artifact, bound installer-log memory,
  make cleanup failures truthful, and invalidate both speech and OCR health for
  shared Google/OpenAI credential changes.
- Run the complete regression suite on staging pushes as well as main, pin CI
  actions to current Node 24 releases, and add static UI contracts for worker
  identity, key purpose, scrolling, focus, and metadata-driven actions.
- Make configuration migration type-safe at every nested boundary, preserve
  malformed inputs for recovery, serialize no-op-aware atomic writes, retain
  valid symlink targets, and reject dangling or non-regular destinations.
- Give selection, capture, OCR, playback, setup, and download jobs verified
  process ownership so Stop/Cancel cannot signal a reused PID or lose a handoff;
  require detached workers to inherit their held job locks, and recover engine
  and voice publication after abrupt termination.
- Bound speech input, captures, catalogues, model artifacts, remote metadata,
  JSON responses, and audio responses; require HTTPS across redirects and
  reject ambiguous or invalid download metadata before network access; keep
  runtime and cache publication from traversing reserved special-file nodes.
- Publish Piper models and sidecars as one verified generation under a shared
  reader/writer lock, repair incomplete installations non-destructively, and
  keep the active voice protected from removal.
- Harden API-key validation and error separation, keep request bodies and keys
  out of argv and logs, normalize remote failure output, and retain only
  privacy-safe, owner-readable telemetry.
- Make speed controls provider-capability-aware, reject unsupported explicit
  overrides, clamp cross-provider persisted preferences safely, improve
  keyboard and assistive labels, render user/service strings as plain text, and
  keep all panel process input JSON-safe.

## 1.2.3 - 2026-09-02

- Add a root `preview.png` for marketplace listing cards.
- Fall back to an owner-only `$XDG_STATE_HOME` directory instead of shared
  `/tmp` when no session runtime directory exists, so runtime PID state is
  never placed where another user could pre-populate it.

## 1.2.2 - 2026-09-02

- Make provider verification silent for every bundled backend and document the
  complete provider environment contract.
- Select ElevenLabs voices from the user's account instead of shipping a stale
  global voice identifier, while preserving existing user choices.
- Keep managed shortcuts attached to the stable Omarchy plugin path when a
  development symlink is retargeted, and retain only the newest three backups.
- Harden runtime-directory, configured-provider, voice-id, and URL-path
  boundaries against symlink and traversal mistakes.
- Apply the configured sanitizer to settings-panel test speech and remove
  redundant long-running status work.
- Consolidate OpenAI voice metadata, reduce `--info` subprocess work, and make
  OCR provider environments consistent across every capture source.
- Require explicit confirmation before cleanup removes shortcuts and API keys.
- Pin direct engine dependencies, keep Kokoro on its supported Python runtime,
  recover truncated health state, serialize concurrent shortcut edits, and
  preserve the voice catalogue when a forced refresh fails.
- Refresh the README, contributor workflow, security guidance, screenshots,
  and marketplace-facing release documentation.

## 1.2.1 - 2026-09-02

- Add local OCR for image-only clipboard content while preferring text when
  both clipboard representations are available.
- Reframe the public documentation around truthful on-demand, local-first
  desktop reading and explicitly distinguish it from a structural screen reader.
- Complete the Quattro bar-widget lifecycle contract and test it in CI.
- Make CLI help and OpenAI's local metadata independent of writable state and
  network tooling.
- Close the setup-worker cancellation race by requiring a verified process
  identity before reporting that installation started.
- Propagate private telemetry write failures instead of reporting stale data as
  a successful account refresh.
- Make settings-state reads network-free and cloud voice refresh explicit.
- Use ElevenLabs' current paginated v2 voice API, with bounded pagination and
  private, atomic account-voice caching.
- Isolate CLI tests from the user's real cache, keyring, and session bus.

## 1.2.0 - 2026-09-02

- Add privacy-safe paid-provider telemetry, live ElevenLabs subscription usage,
  OpenAI rate-limit windows, actionable quota/rate errors, and panel refresh.
- Preserve provider health across transient rate, quota, and service failures,
  and enforce OpenAI's documented 4096-character request boundary locally.
- Make speech replacement race-safe so an older process cannot erase the
  active process's stop/status state.
- Preserve malformed configuration files before recovering with defaults.
- Keep runtime state private and reject runtime directories owned by another
  user.
- Serialize settings writes and foreground actions from the settings panel.
- Validate CLI overrides and captured shortcut syntax at their boundaries.
- Prevent concurrent voice downloads and verify both model size and digest.
- Keep cloud keys and spoken text out of process arguments, keep API errors
  out of the audio path, and bound network waits.
- Pass Kokoro text outside the process environment to support large input and
  reduce exposure through process inspection.
- Refine first-run and provider-selection states around proven readiness.
- Validate process start identities before stopping speech, serialize config
  and health writers, and force-kill providers that ignore verification
  timeouts.
- Serialize setup and voice-download jobs, make repeated voice installation
  non-destructive, and remove progress/completion state races.
- Detect shortcut conflicts against active Hyprland defaults and persist only
  user-owned chord choices so plugin moves cannot leave stale command paths.
- Match Omarchy's bar popout handoff contract and broaden keyboard shortcut
  capture to function, navigation, and punctuation keys.
- Enforce structurally valid one-key shortcuts, stream cloud request text
  without temporary plaintext files, and reject plaintext configuration keys
  so key storage matches the documented keyring-only policy.

## 1.1.0 - 2026-09-02

- Add a complete five-tab settings panel and live speech controls.
- Add graphical first-run setup, provider installation, and keyring entry.
- Add searchable, cancellable Piper voice downloads with integrity checks.
- Add safe shortcut capture, conflict detection, backup, and rollback.
- Add local OCR modes and configurable text sanitization.
- Keep plugin engines and voices in an isolated data directory.

## 1.0.0

- Initial release.
