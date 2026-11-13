# Omarchy TTS Reader (Chrome extension)

Read the current page selection aloud with the TTS provider, voice, and rate
configured in the Omarchy TTS plugin. Audio is synthesized by the local
`speak-bridge` (default `http://127.0.0.1:8787`) and played by the page, so
sentence highlighting, progress, pause/resume, and auto-scroll stay accurate.

This extension never chooses a provider or voice and never overrides the
plugin's rate. It only sends text to `/v1/synthesize` and plays what comes back.

## Requirements

- Chrome or a Chromium-family browser with Manifest V3 support
  (Chrome 105+ for the CSS Custom Highlight API).
- The Omarchy TTS plugin with `bin/speak-bridge` running locally. The bridge
  binds `127.0.0.1` only and requires the pairing token.
- Network access to `http://127.0.0.1/*` (the only host permission).

## Install (unpacked)

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select this `chrome-extension/` directory.
4. Pin **Omarchy TTS Reader** to the toolbar if you want the popup one click
   away.

Chrome may ask to confirm the keyboard shortcuts. The defaults are:

| Action | Shortcut |
| --- | --- |
| Read selection | `Alt+Shift+E` |
| Pause / resume reading | `Alt+Shift+P` |
| Stop reading | `Alt+Shift+X` |
| Open popup | `Alt+Shift+R` |

Shortcuts can be changed at `chrome://extensions/shortcuts`.

## Pairing

1. Start the bridge (`speak-bridge`, or `speak-bridge --install-service`).
2. Run:

   ```bash
   speak-bridge --print-token
   ```

3. Open the extension's **Settings** page (popup → Settings, or the extension
   details → Extension options).
4. Keep the bridge URL at `http://127.0.0.1:8787` unless you started the bridge
   on another port.
5. Paste the token and choose **Test connection**, then **Save**.

The token is stored in `chrome.storage.local`. It is only sent as a
`Authorization: Bearer …` header to the bridge URL, which must be
`http://127.0.0.1:<port>`. The bridge stores the same token in
`${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-tts/browser-token` with mode `0600`.

If the bridge uses a different port, start it with `speak-bridge --port <port>`
and update the URL in Settings. No other host is accepted by this extension.

## Use

- Select text, then press `Alt+Shift+E`, use the popup, or right-click and
  choose **Read selection aloud**.
- The top frame shows a floating HUD with the provider/voice/rate, the current
  sentence, "Sentence i of N", progress, and Pause/Resume/Stop controls.
- Pause works anywhere in a read, including while the next sentence is still
  being synthesized: the reader parks at the sentence playback will resume on,
  and resume continues exactly there (mid-sentence audio keeps its position;
  a sentence that finished synthesizing while paused is not synthesized
  again).
- Pause/resume is available from the HUD, the popup, the `Alt+Shift+P`
  shortcut, and the context menu ("Pause reading" / "Resume reading").
- The selection is split into sentence-sized chunks. Each chunk is synthesized
  just before it plays, and the next chunk is prefetched while the current one
  plays.
- The current sentence is highlighted (CSS Custom Highlight API) and scrolled
  into the middle of the viewport when it leaves a comfortable reading band.
- Only one read runs at a time. Starting another stops the previous one.
- `Alt+Shift+X`, the HUD, the popup, the context menu, or a page navigation
  stops playback and clears the badge.

### Iframe selections

The extension runs in every frame. Starting from the popup or a shortcut probes
all frames and picks the focused selection (or the first non-empty one). The
HUD is always rendered by the top frame, even while audio comes from an iframe.

## What happens on the wire

The extension uses exactly these bridge endpoints:

- `GET /v1/health` — connection test.
- `GET /v1/info` — provider, voice, rate, limits (display only).
- `POST /v1/synthesize` with `{"text":"…"}` — binary audio plus
  `X-Tts-Provider`, `X-Tts-Voice`, `X-Tts-Rate`, `X-Tts-Format`.
- `POST /v1/stop` — ask the bridge to stop an in-flight synthesis.

All HTTP calls happen in the MV3 service worker. Content scripts never hold the
token and never make cross-origin requests. See [PROTOCOL.md](PROTOCOL.md) for
message shapes, statuses, timeouts, and error handling.

## Permissions

| Permission | Why |
| --- | --- |
| `contextMenus` | "Read selection aloud", "Pause/Resume reading" and "Stop reading" menu items. |
| `storage` | Bridge URL/token in `local`, active read in `session`. |
| `scripting` | Probe frames for a selection when starting from the popup/shortcut. |
| `activeTab` | Read the page the user explicitly invoked the extension on. |
| `http://127.0.0.1/*` | Reach the local bridge only. |

The extension does not request `tabs`, does not inject remotely hosted code,
and does not log page text or audio.

## Troubleshooting

**"Cannot reach the TTS bridge…"**
Start it with `speak-bridge` and confirm `speak-bridge --status`. Check the URL
in Settings. The bridge must be listening on `127.0.0.1`.

**"The bridge rejected the token"**
Print a fresh token with `speak-bridge --print-token` and paste it into
Settings. Tokens can be rotated with `speak-bridge --rotate-token`.

**"Select some text first"**
The shortcut/popup probes all frames. Make a non-collapsed selection in a
normal `http(s)` page. Some pages block script injection.

**The HUD says audio was blocked**
Browser autoplay policy can block the first asynchronous playback. Click
**Resume** in the HUD, or allow audio for the site, then try again.

**No highlighting inside a text field**
Selections inside `input`/`textarea` are read through the HUD only; there is no
DOM range to highlight. This is a documented limitation.

**Chrome's own pages do nothing**
`chrome://` pages, the Chrome Web Store, the built-in PDF viewer, and other
protected pages do not allow content scripts. Extension error pages also cannot
be read.

## Known limitations

- Protected browser pages: `chrome://*`, the Web Store, and Chrome's PDF viewer.
- Shadow DOM (open or closed) on the page: selection text may not be visible to
  the content script's DOM walk, so highlighting can be unavailable.
- In-place highlighting inside `input`/`textarea` (audio + HUD still work).
- One active read at a time, per browser profile.
- Auto-scroll uses a temporary zero-size marker and `scrollIntoView`; a page
  that aggressively rewrites its DOM during reading may lose highlighting.
- Pages that block extension content scripts cannot be read.
- The bridge caps request text at 20,000 characters; the extension sends
  chunks of at most 400 characters, so this is normally invisible.
- The extension caps a selection at 100,000 characters and says so in the HUD.

## Development checks

From the repository root:

```bash
for f in chrome-extension/*.js; do node --check "$f"; done
jq -e . chrome-extension/manifest.json
```

Manual smoke test:

1. Load the unpacked extension and pair it with the running bridge.
2. Open a long article, select several paragraphs, press `Alt+Shift+E`.
3. Confirm audio, orange sentence highlighting, progress, auto-scroll, and
   provider/voice/rate in the HUD.
4. Pause, resume, stop. Confirm the badge (`▶`) appears only while reading.
5. Select text inside an iframe, start from the popup, and confirm the HUD is
   rendered by the top frame.
6. Navigate away mid-read and confirm playback stops and the badge clears.
7. Stop the bridge, start a read, and confirm the HUD error points to Settings.

## Files

- `manifest.json` — MV3 manifest.
- `shared.js` — constants and pure helpers, loaded by every context.
- `background.js` — service worker: HTTP, state, badge, menus, commands.
- `content.js` — selection capture, chunking, highlights, playback, HUD.
- `content.css` — `::highlight()` paint styles.
- `popup.html/.js/.css` — toolbar popup.
- `options.html/.js/.css` — bridge pairing and connection test.
- `PROTOCOL.md` — HTTP + extension message protocol.
