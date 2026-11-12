# Protocol

Two protocols meet here: the plugin's local HTTP contract and the extension's
internal message contract. The extension implements the first without adding
endpoints or overrides, and keeps all HTTP in the service worker.

## 1. Local HTTP bridge

Base URL: `http://127.0.0.1:8787` by default. The extension accepts only
`http://127.0.0.1:<port>` (no other host, no query, no credentials, HTTP only).

Every request carries:

```
Authorization: Bearer <token>
```

The token comes from `speak-bridge --print-token` and is stored in
`chrome.storage.local`. The bridge allows the extension origin
(`chrome-extension://…`) and answers CORS preflight; the extension sends no
cookies (`credentials: omit`) and does not follow redirects.

Timeouts used by the service worker:

| Call | Timeout |
| --- | --- |
| `GET /v1/health`, `GET /v1/info` | 5 s |
| `POST /v1/synthesize` | 130 s |
| `POST /v1/stop` | 5 s, fire-and-forget |

### `GET /v1/health`

Connection test. Expected:

```json
{ "ok": true, "bridgeVersion": "0.1.0", "pluginVersion": "1.5.0", "speaking": false }
```

A response that is not HTTP 200 or has `ok !== true` is a connection failure.

### `GET /v1/info`

Used for display only (provider, voice, rate, limits). Expected fields:

```json
{
  "provider": "piper",
  "voice": "en_US-amy-medium",
  "rate": 1.0,
  "maxChars": 0,
  "limits": { "maxChars": 200000, "maxBytes": 1048576 },
  "bridgeVersion": "0.1.0"
}
```

The extension trims these fields defensively and never uses them to override
provider/voice/rate.

### `POST /v1/synthesize`

Request body, exactly one field:

```json
{ "text": "A sentence to read." }
```

No provider, voice, or rate overrides are sent.

Success `200`:

- Body: audio bytes.
- `Content-Type`: `audio/wav`, `audio/mpeg`, or `audio/ogg`.
- `X-Tts-Provider`, `X-Tts-Voice`, `X-Tts-Rate`, `X-Tts-Format`
  (`wav|mp3|ogg`).
- `Cache-Control: no-store`.

Other outcomes:

- `204`: nothing speakable. The extension skips the chunk; four empty chunks
  in a row become a visible "no speakable audio" error.
- `400` / `413` / `502` / `504`: JSON `{ "error": "...", "message": "..." }`.
  The bridge message is shown in the HUD, bounded and whitespace-collapsed.
  The response never contains the selected text.

The extension additionally rejects audio larger than 16 MiB to protect memory,
and derives the MIME type from `X-Tts-Format` if `Content-Type` is missing.

### `POST /v1/stop`

No body. Expected `{"ok":true}` after the bridge runs `speak --stop`. The
extension calls this on explicit stop, new read, and tab cleanup. The call is
never allowed to delay starting the next read.

## 2. Extension messages

All messages are plain objects with a string `type`. Extension messages are
only accepted from the extension itself. Page content is never trusted as
HTML; text is inserted with `textContent`.

### Content script → service worker

#### `SYNTHESIZE`

```js
{ type: "SYNTHESIZE", text: "..." }
```

Only accepted from the currently active reading frame. Text is capped at
20,000 characters (chunks are normally ≤ 400).

Responses:

```js
{ ok: true, mime: "audio/wav", b64: "...", provider: "piper", voice: "...", rate: 1.0 }
{ ok: true, empty: true }                       // bridge returned 204
{ ok: false, error: "unreachable", message: "..." }
```

#### `BRIDGE_INFO`

```js
{ type: "BRIDGE_INFO" }
→ { ok: true, info: { provider, voice, rate, maxChars, limits, bridgeVersion } }
→ { ok: false, error, message }
```

#### `READ_STARTED`

```js
{ type: "READ_STARTED", total: 12, provider: null, voice: null, rate: null }
```

Sent after capture/chunking. Metadata may still be `null`; it is filled in from
the first synthesis response or a concurrent info request. `paused: true` is
included when the read was paused before the first chunk could start, so the
worker does not overwrite the paused state with `preparing`.

#### `READ_STATE`

```js
{
  type: "READ_STATE",
  state: {
    status: "preparing" | "speaking" | "paused" | "done" | "error",
    index: 1,              // 1-based current chunk; 0 while preparing
    total: 12,
    text: "current sentence",   // current chunk only, never the whole selection
    provider: "piper",
    voice: "en_US-amy-medium",
    rate: 1.0,
    message: "optional error or notice",
    progress: 0.42         // optional, current chunk fraction 0..1
  }
}
```

Sent on start, chunk change, pause/resume, done, error, and at 4 Hz while
playing. The service worker validates all fields, caps strings, and forwards a
normalized state to frame `0` of the reading tab.

#### `READ_GONE`

```js
{ type: "READ_GONE" }
```

Sent from `pagehide` when the reading document unloads. The worker clears the
active read, badge, and (if possible) the HUD.

#### `START_READ` / `STOP_READ` (content origin)

Accepted for testability and page-context automation:

```js
{ type: "START_READ", text: "optional fallback text" }
{ type: "STOP_READ" }
```

When `sender.tab` is present, the worker starts/stops using
`sender.tab.id` and `sender.frameId` exactly — it does not use the active tab.
A content-origin `STOP_READ` only stops a read in that same tab/frame.

### Service worker → content scripts

#### `START_READ`

```js
{ type: "START_READ", text: "optional fallback text" }
```

Targeted at one frame. The content script captures the live selection, or uses
`text` when the DOM selection is gone. It responds with `{ok:true,total}` or a
`{ok:false,error,message}` result and reports any error through `READ_STATE`.

#### `STOP_READ`

```js
{ type: "STOP_READ" }
```

Stops audio, revokes the object URL, cancels prefetch, clears highlights, and
hides the HUD if this is the top frame. No `READ_STATE` is sent for an explicit
stop; the worker has already broadcast the stopped state.

#### `HUD_CONTROL` (forwarded)

```js
{ type: "HUD_CONTROL", action: "pause" | "resume" | "toggle-pause" | "stop" | "close" }
```

Sent from the worker to the active reading frame after a validated HUD
request, or from the `toggle-pause` command, the popup's `PAUSE_TOGGLE`, or
the context-menu pause item. `toggle-pause` lets the reading frame decide the
direction from its own state.

#### `HUD_STATE`

```js
{ type: "HUD_STATE", state: { ...same shape as READ_STATE state... } }
```

Sent only to frame `0` (the top frame). Content scripts in subframes ignore it.
The top frame renders the HUD in a **closed** shadow root
(`mode: "closed"`) on a host with `id="omarchy-tts-hud"`. Page scripts cannot
read the panel contents or drive its buttons; only the content script's own
reference can. Styling is isolated with `all: initial` and a high `z-index`.

Observable host state (useful for tests and debugging):

| Attribute | Meaning |
| --- | --- |
| `data-status` | Current status. |
| `data-index` | Current 1-based chunk. |
| `data-total` | Total chunks. |
| `data-provider` | Provider when known. |
| `data-voice` | Voice when known. |

HUD rules:

- Visible for `preparing`, `speaking`, `paused`, and `error`.
- `done` hides after ~4 seconds.
- `done` with `state.stopped === true` hides immediately (explicit stop).
- Error stays visible until closed, stopped, or a new read starts.
- Provider/voice/rate, the current sentence (clamped to three lines),
  "Sentence i of N", and a progress bar are shown.
- Settings opens the options page via `OPEN_OPTIONS`.

### Top frame → service worker

#### `HUD_CONTROL`

```js
{ type: "HUD_CONTROL", action: "pause" | "resume" | "stop" | "close" }
```

Only accepted from frame `0` of the active reading tab.

- `pause` / `resume`: forwarded to the reading frame.
- `stop` / `close`: treated as a stop. The HUD hides immediately; the worker
  clears the badge, tells the reading frame to stop, and posts `/v1/stop`.

### Popup / options → service worker

#### `GET_READ_STATE`

```js
→ { ok: true, active: true|false, state: { ... } | null }
```

Used by the popup for polling. `active` reflects the live `activeRead` record,
not the status string.

#### `PAUSE_TOGGLE`

```js
→ { ok: true }
→ { ok: false, error: "not_active", message: "Nothing is reading." }
```

Popup origin. Toggles pause/resume in the active reading frame; the frame
decides the direction. Also reachable through the `toggle-pause` command
(`Alt+Shift+P`) and the "Pause reading"/"Resume reading" context menu item,
whose label follows the last reported state.

#### `START_READ` / `STOP_READ`

Popup origin (`sender.tab` absent):

- `START_READ`: query the active tab, probe every frame for a non-collapsed
  selection (prefer `document.hasFocus()`), then start in the winning
  `frameId`.
- `STOP_READ`: stop the active read.

#### `TEST_BRIDGE`

```js
{ type: "TEST_BRIDGE" }
// or, for unsaved options-form values:
{ type: "TEST_BRIDGE", bridgeUrl: "http://127.0.0.1:8787", bridgeToken: "..." }
→ { ok: true, info: { provider, voice, rate, ... } }
→ { ok: false, error, message }
```

Calls `/v1/health` then `/v1/info`. Overrides are validated with the same
`127.0.0.1` rule and are never persisted by the test.

#### `OPEN_OPTIONS`

```js
{ type: "OPEN_OPTIONS" }
```

Opens `options.html`. Also accepted from content scripts (HUD Settings link).

## 3. Worker state

`chrome.storage.local`:

```js
{ bridgeUrl: "http://127.0.0.1:8787", bridgeToken: "..." }
```

`chrome.storage.session`:

```js
activeRead: { tabId: 12, frameId: 0, startedAt: 1700000000000 }
lastState:  { ...last normalized state... }
```

`activeRead` is the single-read lock. `lastState` survives a service-worker
restart so the popup/HUD can recover. Progress updates are kept in memory and
are not written to storage 4 times per second.

Badge: `▶` on the reading tab while a read is preparing/speaking/paused. Cleared
on `done`, `error`, explicit stop, tab close, and top-level navigation.

## 4. Flows

### Start from popup or shortcut

1. Worker probes `document.getSelection()` and focused `input`/`textarea`
   selections in all frames.
2. Worker stops any previous read, records `activeRead`, sets the badge.
3. Worker sends `START_READ` to the winning frame.
4. Content captures a `Range`, builds canonical text + offset map, splits with
   `Intl.Segmenter` (regex fallback) into ≤ 400-character chunks.
5. Content sends `READ_STARTED`, then `READ_STATE` `preparing`.
6. For each chunk: `SYNTHESIZE` → Blob URL → `Audio.play()`; current highlight
   and auto-scroll are applied; the next chunk is prefetched.
7. `READ_STATE` with `progress` is sent every 250 ms while speaking.
8. `done` clears the badge and hides the HUD after ~4 s. `error` keeps the HUD
   visible with the bridge message.

### Context menu

Uses `info.frameId` directly (no probing). Passes `info.selectionText` as a
fallback so a selection that collapses while the menu closes can still be read.

### Pause / resume

Pause is a content-script session flag, not a bridge call (a synthesis already
in flight finishes and stays cached). At any point in the read:

- While a sentence plays: the `<audio>` element pauses in place and resumes
  from the same position.
- While the next chunk is synthesizing, or in the gap between chunks: the
  walker parks at the pending chunk (`pendingIndex`) and reports `paused`.
  The parked state's `index`/highlight name the sentence playback will resume
  on. Resume continues there without a second synthesis (the completed
  synthesis stays cached).
- While `preparing` (before the first chunk): the walker parks before any
  synthesis; nothing is synthesized until resume.
- The context menu label follows the reported state ("Pause reading" /
  "Resume reading").

### Iframe selection

Content runs in the iframe and sends states with its `frameId`. The worker
forwards every `HUD_STATE` to frame `0`, so the HUD always appears on the top
page. Content in the iframe does not create a HUD.

### Stop paths

All of these stop audio, revoke the current object URL, cancel prefetch, clear
highlights, clear the badge, and post `/v1/stop`:

- `Alt+Shift+X`, popup Stop, context menu Stop.
- HUD Stop or Close.
- Starting a new read.
- Tab close.
- Top-level navigation (`tabs.onUpdated` status `loading`).
- Iframe/document unload (`READ_GONE`).

## 5. Error codes

HTTP/worker errors surface as `{ ok:false, error, message }`. Common values:

| Code | Meaning |
| --- | --- |
| `unreachable` | Bridge not running / wrong port. |
| `no_token` | No token saved; points to Settings. |
| `unauthorized` | Bridge rejected the token. |
| `forbidden` | Origin rejected. |
| `bad_url` | URL is not `http://127.0.0.1:<port>`. |
| `timeout` | Bridge or synthesis timed out. |
| `too_large` | Text/audio exceeded a limit. |
| `synthesis_failed` | Bridge reported a synthesis failure. |
| `unsupported_audio` | Bridge returned an unplayable format. |
| `no_selection` | No non-empty selection found. |
| `no_content` | Frame has no reachable content script. |
| `not_active` | A stale frame tried to act. |
| `empty_audio` | Repeated 204 responses. |
| `audio_error` | Browser could not decode/play returned audio. |

The HUD shows the bounded `message`; nothing logs selection text or audio.
