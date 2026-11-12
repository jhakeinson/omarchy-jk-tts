/*
 * Omarchy TTS Reader - MV3 service worker.
 *
 * All HTTP calls to the local bridge happen here so content scripts never need
 * CORS or network permissions of their own.
 */
importScripts('shared.js');

'use strict';

var T = self.OmarchyTTS;

var MENU_READ = 'omarchy-tts-read-selection';
var MENU_STOP = 'omarchy-tts-stop-reading';

/* {tabId, frameId, startedAt} - mirrored in chrome.storage.session. */
var activeRead = null;
/* Last state shown by the HUD / popup. Progress updates stay in memory. */
var lastState = null;
var lastStateStatus = '';

/* ------------------------------------------------------------------ */
/* Storage                                                             */
/* ------------------------------------------------------------------ */

async function ensureDefaults() {
  try {
    var stored = await chrome.storage.local.get(['bridgeUrl', 'bridgeToken']);
    var patch = {};
    if (typeof stored.bridgeUrl !== 'string' || !stored.bridgeUrl.trim()) {
      patch.bridgeUrl = T.DEFAULTS.bridgeUrl;
    }
    if (typeof stored.bridgeToken !== 'string') {
      patch.bridgeToken = T.DEFAULTS.bridgeToken;
    }
    if (Object.keys(patch).length > 0) {
      await chrome.storage.local.set(patch);
    }
  } catch (err) {
    /* Storage may be unavailable during shutdown; nothing to recover. */
  }
}

function persistActive() {
  try {
    if (activeRead) {
      chrome.storage.session.set({ activeRead: activeRead }).catch(function () {});
    } else {
      chrome.storage.session.remove('activeRead').catch(function () {});
    }
  } catch (err) {
    /* ignore */
  }
}

function persistLastState() {
  if (!lastState) return;
  try {
    chrome.storage.session
      .set({ lastState: lastState })
      .catch(function () {});
  } catch (err) {
    /* ignore */
  }
}

var restorePromise = null;

function restoreSession() {
  if (!restorePromise) {
    restorePromise = (async function () {
      try {
        var stored = await chrome.storage.session.get([
          'activeRead',
          'lastState'
        ]);
        if (stored.lastState && typeof stored.lastState === 'object') {
          lastState = stored.lastState;
          lastStateStatus = T.safeText(lastState.status, 20);
        }
        /* A read created while restore was pending always wins. */
        if (activeRead) return;
        var candidate = stored.activeRead;
        if (
          candidate &&
          typeof candidate === 'object' &&
          typeof candidate.tabId === 'number' &&
          typeof candidate.frameId === 'number'
        ) {
          try {
            await chrome.tabs.get(candidate.tabId);
            /* The frame may be gone after a navigation while we slept. */
            await chrome.tabs.sendMessage(
              candidate.tabId,
              { type: 'READ_PING' },
              { frameId: candidate.frameId }
            );
            activeRead = {
              tabId: candidate.tabId,
              frameId: candidate.frameId,
              url: T.safeText(candidate.url, 2048),
              startedAt: T.isFiniteNumber(candidate.startedAt)
                ? candidate.startedAt
                : Date.now()
            };
            await setBadge(activeRead.tabId);
          } catch (err) {
            activeRead = null;
            persistActive();
            await clearBadge(candidate.tabId);
          }
        }
      } catch (err) {
        /* ignore */
      }
    })();
  }
  return restorePromise;
}

/* ------------------------------------------------------------------ */
/* Bridge HTTP                                                         */
/* ------------------------------------------------------------------ */

async function getSettings(overrides) {
  var stored = await chrome.storage.local.get({
    bridgeUrl: T.DEFAULTS.bridgeUrl,
    bridgeToken: T.DEFAULTS.bridgeToken
  });

  var rawUrl =
    overrides && typeof overrides.bridgeUrl === 'string'
      ? overrides.bridgeUrl
      : stored.bridgeUrl;
  var rawToken =
    overrides && typeof overrides.bridgeToken === 'string'
      ? overrides.bridgeToken
      : stored.bridgeToken;

  var parsed = T.parseBridgeUrl(rawUrl);
  if (!parsed) {
    throw T.makeBridgeError(
      'bad_url',
      'The bridge URL must be http://127.0.0.1:<port>. Open Settings to fix it.'
    );
  }

  var token = typeof rawToken === 'string' ? rawToken.trim() : '';
  if (!token) {
    throw T.makeBridgeError(
      'no_token',
      'No bridge token is configured. Open Settings and paste the token from `speak-bridge --print-token`.'
    );
  }

  return { base: T.bridgeBase(parsed), token: token };
}

async function openBridge(settings, path, options) {
  var opts = options || {};
  var controller = new AbortController();
  var timeoutMs = T.isFiniteNumber(opts.timeoutMs)
    ? opts.timeoutMs
    : T.TIMEOUTS.infoMs;
  var timer = setTimeout(function () {
    controller.abort();
  }, timeoutMs);

  var headers = Object.assign(
    { Authorization: 'Bearer ' + settings.token },
    opts.headers || {}
  );

  try {
    return await fetch(settings.base + path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body,
      signal: controller.signal,
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error'
    });
  } catch (err) {
    if (controller.signal.aborted) {
      throw T.makeBridgeError(
        'timeout',
        'The TTS bridge did not answer in time. Check that it is running and try again.'
      );
    }
    throw T.makeBridgeError(
      'unreachable',
      'Cannot reach the TTS bridge at ' +
        settings.base +
        '. Start it with `speak-bridge`, then check Settings.'
    );
  } finally {
    clearTimeout(timer);
  }
}

async function throwForResponse(response) {
  var body = '';
  try {
    body = await response.text();
  } catch (err) {
    body = '';
  }
  body = body.slice(0, 4096);
  var parsed = T.parseErrorBody(body);

  if (response.status === 401) {
    throw T.makeBridgeError(
      'unauthorized',
      'The bridge rejected the token. Copy a fresh token with `speak-bridge --print-token` into Settings.',
      401
    );
  }
  if (response.status === 403) {
    throw T.makeBridgeError(
      'forbidden',
      'The bridge rejected this extension origin. Check the bridge version and settings.',
      403
    );
  }
  if (response.status === 413) {
    throw T.makeBridgeError(
      'too_large',
      parsed.message || 'That selection is too large for the TTS bridge.',
      413
    );
  }
  if (response.status === 504) {
    throw T.makeBridgeError(
      'timeout',
      parsed.message || 'The TTS bridge timed out while synthesizing audio.',
      504
    );
  }

  var code = parsed.error;
  if (!code) {
    code = response.status >= 500 ? 'synthesis_failed' : 'bad_response';
  }
  var message =
    parsed.message ||
    'The bridge returned HTTP ' + response.status + '. Check Settings and try again.';
  throw T.makeBridgeError(code, T.trimMessage(message, 600), response.status);
}

function trimInfo(data) {
  var src = data && typeof data === 'object' ? data : {};
  var limits =
    src.limits && typeof src.limits === 'object' ? src.limits : {};
  return {
    provider: T.safeText(src.provider, 120),
    voice: T.safeText(src.voice, 160),
    rate: T.clampNumber(src.rate, 0, 10),
    maxChars: T.clampInteger(src.maxChars, 0, 10000000),
    limits: {
      maxChars: T.clampInteger(limits.maxChars, 0, 10000000),
      maxBytes: T.clampInteger(limits.maxBytes, 0, 100000000)
    },
    bridgeVersion: T.safeText(src.bridgeVersion, 40)
  };
}

async function getHealth(settings) {
  var response = await openBridge(settings, T.API.health, {
    timeoutMs: T.TIMEOUTS.infoMs
  });
  if (!response.ok) {
    await throwForResponse(response);
  }
  var data = null;
  try {
    data = await response.json();
  } catch (err) {
    throw T.makeBridgeError(
      'bad_response',
      'The bridge returned an invalid health response.'
    );
  }
  if (!data || data.ok !== true) {
    throw T.makeBridgeError(
      'bridge_error',
      'The bridge reported that it is not healthy.'
    );
  }
  return data;
}

async function getInfo(settings) {
  var response = await openBridge(settings, T.API.info, {
    timeoutMs: T.TIMEOUTS.infoMs
  });
  if (!response.ok) {
    await throwForResponse(response);
  }
  var data = null;
  try {
    data = await response.json();
  } catch (err) {
    throw T.makeBridgeError(
      'bad_response',
      'The bridge returned an invalid info response.'
    );
  }
  return trimInfo(data);
}

function requestBridgeStop() {
  /*
   * Serialized with the caller: the bridge's stop and the next synthesis must
   * not race, or a late stop can kill the new engine process.
   */
  return getSettings()
    .then(function (settings) {
      return openBridge(settings, T.API.stop, {
        method: 'POST',
        timeoutMs: T.TIMEOUTS.stopMs
      });
    })
    .catch(function () {
      /* The bridge may not be running at all. */
    });
}

async function synthesize(message, sender) {
  if (!sender.tab || typeof sender.frameId !== 'number') {
    return {
      ok: false,
      error: 'bad_request',
      message: 'Synthesis must come from a page content script.'
    };
  }
  if (
    !activeRead ||
    activeRead.tabId !== sender.tab.id ||
    activeRead.frameId !== sender.frameId
  ) {
    return {
      ok: false,
      error: 'not_active',
      message: 'This frame is not the active reading frame.'
    };
  }

  var text = typeof message.text === 'string' ? message.text : '';
  if (!text.trim()) {
    return {
      ok: false,
      error: 'bad_request',
      message: 'There is no text to synthesize.'
    };
  }
  if (text.length > T.MAX_SYNTH_CHARS) {
    return {
      ok: false,
      error: 'too_large',
      message: 'That text chunk is larger than the bridge accepts.'
    };
  }

  var settings = await getSettings();
  var response = await openBridge(settings, T.API.synthesize, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text }),
    timeoutMs: T.TIMEOUTS.synthesizeMs
  });

  if (response.status === 204) {
    return { ok: true, empty: true };
  }
  if (!response.ok) {
    await throwForResponse(response);
  }

  var mime = T.safeText(response.headers.get('content-type'), 80)
    .split(';')[0]
    .trim()
    .toLowerCase();
  var format = T.safeText(response.headers.get('x-tts-format'), 20);
  if (!Object.prototype.hasOwnProperty.call(T.AUDIO_MIME, mime)) {
    mime = T.mimeForFormat(format);
  }
  if (!Object.prototype.hasOwnProperty.call(T.AUDIO_MIME, mime)) {
    throw T.makeBridgeError(
      'unsupported_audio',
      'The bridge returned an audio format the browser cannot play.'
    );
  }

  var lengthHeader = Number(response.headers.get('content-length'));
  if (isFinite(lengthHeader) && lengthHeader > T.MAX_AUDIO_BYTES) {
    throw T.makeBridgeError(
      'too_large',
      'The synthesized audio is too large to play.'
    );
  }

  var buffer = await response.arrayBuffer();
  if (!buffer.byteLength) {
    return { ok: true, empty: true };
  }
  if (buffer.byteLength > T.MAX_AUDIO_BYTES) {
    throw T.makeBridgeError(
      'too_large',
      'The synthesized audio is too large to play.'
    );
  }

  var rateHeader = response.headers.get('x-tts-rate');
  var rate = rateHeader === null ? null : Number(rateHeader);
  if (!isFinite(rate) || rate <= 0) rate = null;
  return {
    ok: true,
    mime: mime,
    b64: T.bytesToBase64(new Uint8Array(buffer)),
    provider: T.safeText(response.headers.get('x-tts-provider'), 120),
    voice: T.safeText(response.headers.get('x-tts-voice'), 160),
    rate: rate
  };
}

/* ------------------------------------------------------------------ */
/* Badge                                                               */
/* ------------------------------------------------------------------ */

async function setBadge(tabId) {
  if (typeof tabId !== 'number') return;
  try {
    await chrome.action.setBadgeBackgroundColor({ color: '#f97316' });
    await chrome.action.setBadgeText({ tabId: tabId, text: '\u25b6' });
  } catch (err) {
    /* Tab may already be gone. */
  }
}

async function clearBadge(tabId) {
  if (typeof tabId !== 'number') return;
  try {
    await chrome.action.setBadgeText({ tabId: tabId, text: '' });
  } catch (err) {
    /* Tab may already be gone. */
  }
}

/* ------------------------------------------------------------------ */
/* HUD forwarding                                                      */
/* ------------------------------------------------------------------ */

function sendToFrame(tabId, frameId, message) {
  return chrome.tabs
    .sendMessage(tabId, message, { frameId: frameId })
    .catch(function () {
      return null;
    });
}

function sendToTop(tabId, message) {
  return sendToFrame(tabId, 0, message);
}

function normalizeState(state) {
  var src = state && typeof state === 'object' ? state : {};
  var status = T.STATUSES.indexOf(src.status) >= 0 ? src.status : 'error';
  var result = {
    status: status,
    index: T.clampInteger(src.index, 0, 1000000) || 0,
    total: T.clampInteger(src.total, 0, 1000000) || 0,
    text: T.safeText(src.text, 2000),
    provider: T.safeText(src.provider, 120),
    voice: T.safeText(src.voice, 160),
    rate: T.clampNumber(src.rate, 0, 10)
  };
  if (T.isFiniteNumber(src.progress)) {
    result.progress = T.clampNumber(src.progress, 0, 1);
  }
  var message = T.trimMessage(src.message, 1000);
  if (message) result.message = message;
  if (src.stopped === true) result.stopped = true;
  return result;
}

function rememberState(state, persist) {
  lastState = state;
  if (persist || state.status !== lastStateStatus) {
    lastStateStatus = state.status;
    persistLastState();
  }
}

function forwardHud(tabId, state) {
  if (typeof tabId !== 'number') return;
  sendToTop(tabId, { type: 'HUD_STATE', state: state });
}

/* ------------------------------------------------------------------ */
/* Reading lifecycle                                                   */
/* ------------------------------------------------------------------ */

async function stopActiveLocked(options) {
  await restoreSession();
  var opts = options || {};
  var previous = activeRead;
  activeRead = null;

  var doneState = {
    status: 'done',
    index: 0,
    total: 0,
    text: '',
    provider: null,
    voice: null,
    rate: null,
    stopped: true
  };
  rememberState(doneState, true);
  persistActive();

  if (previous) {
    await clearBadge(previous.tabId);
    if (opts.notify !== false) {
      sendToFrame(previous.tabId, previous.frameId, { type: 'STOP_READ' });
      forwardHud(previous.tabId, doneState);
    }
  }

  if (previous && opts.bridgeStop !== false) {
    await requestBridgeStop();
  }
  return { ok: true };
}

async function startReadLocked(tabId, frameId, options) {
  await restoreSession();
  var opts = options || {};
  if (typeof tabId !== 'number' || typeof frameId !== 'number') {
    return {
      ok: false,
      error: 'bad_request',
      message: 'No page frame was selected.'
    };
  }

  if (activeRead) {
    await stopActiveLocked({ reason: 'replace', notify: true });
  }

  activeRead = { tabId: tabId, frameId: frameId, startedAt: Date.now() };
  try {
    var tab = await chrome.tabs.get(tabId);
    if (tab && typeof tab.url === 'string') {
      activeRead.url = T.safeText(tab.url, 2048);
    }
  } catch (err) {
    /* The tab may have gone away between the trigger and here. */
  }
  var preparing = {
    status: 'preparing',
    index: 0,
    total: 0,
    text: '',
    provider: null,
    voice: null,
    rate: null
  };
  rememberState(preparing, true);
  persistActive();
  await setBadge(tabId);

  var message = { type: 'START_READ' };
  if (typeof opts.fallbackText === 'string' && opts.fallbackText.trim()) {
    message.text = opts.fallbackText.slice(0, T.MAX_SELECTION_CHARS);
  }

  var response;
  try {
    response = await chrome.tabs.sendMessage(tabId, message, {
      frameId: frameId
    });
  } catch (err) {
    response = null;
  }

  if (!response || response.ok !== true) {
    /* Content scripts report their own error HUD state when they can. */
    if (!response) {
      await stopActiveLocked({ reason: 'start_failed', notify: false });
      return {
        ok: false,
        error: 'no_content',
        message:
          'Cannot start reading in this frame. Reload the page and try again.'
      };
    }
    /* The content script already reported the error state for the HUD. */
    activeRead = null;
    persistActive();
    await clearBadge(tabId);
    return response;
  }
  return { ok: true };
}

/*
 * Serialize start/stop so two simultaneous triggers cannot both become the
 * active read. Locked variants above call each other directly to avoid
 * deadlocking on this queue.
 */
var lifecycle = Promise.resolve();

function withLifecycle(task) {
  var run = lifecycle.then(task, task);
  lifecycle = run.then(
    function () {},
    function () {}
  );
  return run;
}

function stopActive(options) {
  return withLifecycle(function () {
    return stopActiveLocked(options);
  });
}

function startRead(tabId, frameId, options) {
  return withLifecycle(function () {
    return startReadLocked(tabId, frameId, options);
  });
}

async function probeSelection(tabId) {
  var results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: tabId, allFrames: true },
      func: function () {
        try {
          var text = '';
          var selection = window.getSelection();
          if (selection && selection.rangeCount > 0 && !selection.isCollapsed) {
            text = selection.toString();
          }
          if (!text.trim()) {
            var el = document.activeElement;
            var autocomplete = String(
              (el && el.autocomplete) || ''
            ).toLowerCase();
            var isTextField =
              el &&
              !/(^|\s)(current-password|new-password|one-time-code|cc-\w+)/.test(
                autocomplete
              ) &&
              (el.tagName === 'TEXTAREA' ||
                (el.tagName === 'INPUT' &&
                  /^(text|search|url|tel|email)$/i.test(
                    el.type || 'text'
                  )));
            if (isTextField) {
              var start = el.selectionStart;
              var end = el.selectionEnd;
              if (
                typeof start === 'number' &&
                typeof end === 'number' &&
                end > start
              ) {
                text = el.value.slice(start, end);
              }
            }
          }
          if (!text.trim()) return null;
          return {
            text: text.slice(0, 100000),
            focused: document.hasFocus()
          };
        } catch (err) {
          return null;
        }
      }
    });
  } catch (err) {
    return {
      ok: false,
      error: 'cannot_probe',
      message:
        'This page does not allow reading (browser pages and the Web Store are blocked). Open a normal http(s) page and select text.'
    };
  }

  var winner = null;
  for (var i = 0; i < (results || []).length; i += 1) {
    var item = results[i];
    if (!item || !item.result) continue;
    if (item.result.focused) {
      winner = item;
      break;
    }
    if (!winner) winner = item;
  }

  if (!winner) {
    return {
      ok: false,
      error: 'no_selection',
      message: 'Select some text first, then try again.'
    };
  }

  return {
    ok: true,
    frameId: typeof winner.frameId === 'number' ? winner.frameId : 0,
    text: T.safeText(winner.result.text, T.MAX_SELECTION_CHARS)
  };
}

async function getActiveTab() {
  var tabs = await chrome.tabs.query({
    active: true,
    lastFocusedWindow: true
  });
  if (!tabs.length) {
    tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  }
  return tabs.length ? tabs[0] : null;
}

async function startFromActiveTab() {
  var tab = await getActiveTab();
  if (!tab || typeof tab.id !== 'number') {
    return {
      ok: false,
      error: 'no_tab',
      message: 'No active tab was found.'
    };
  }
  var probe = await probeSelection(tab.id);
  if (!probe.ok) return probe;
  return startRead(tab.id, probe.frameId, {
    fallbackText: probe.text,
    source: 'active-tab'
  });
}

/* ------------------------------------------------------------------ */
/* Message handlers                                                    */
/* ------------------------------------------------------------------ */

function failure(err) {
  if (T.isBridgeError(err)) {
    return { ok: false, error: err.code, message: err.message };
  }
  return {
    ok: false,
    error: 'internal',
    message: 'The extension hit an unexpected error.'
  };
}

async function onReadStarted(message, sender) {
  if (!sender.tab || typeof sender.frameId !== 'number') {
    return { ok: false, error: 'bad_request', message: 'No reading frame.' };
  }

  if (
    !activeRead ||
    activeRead.tabId !== sender.tab.id ||
    activeRead.frameId !== sender.frameId
  ) {
    return {
      ok: false,
      error: 'not_active',
      message: 'Another frame is already reading.'
    };
  }

  var state = {
    status: 'preparing',
    index: 0,
    total: T.clampInteger(message.total, 0, 1000000) || 0,
    text: '',
    provider: T.safeText(message.provider, 120),
    voice: T.safeText(message.voice, 160),
    rate: T.clampNumber(message.rate, 0, 10)
  };
  rememberState(state, true);
  await setBadge(sender.tab.id);
  forwardHud(sender.tab.id, state);
  return { ok: true };
}

async function onReadState(message, sender) {
  if (!sender.tab || typeof sender.frameId !== 'number') {
    return { ok: false, error: 'bad_request', message: 'No reading frame.' };
  }

  var state = normalizeState(message.state);

  if (!activeRead) {
    /* Surface an early error (for example "select text first") in the HUD,
     * including when the reader lives in an iframe. */
    if (state.status === 'error' && sender.tab) {
      rememberState(state, true);
      forwardHud(sender.tab.id, state);
    }
    return { ok: false, error: 'not_active', message: 'No active read.' };
  }

  if (
    activeRead.tabId !== sender.tab.id ||
    activeRead.frameId !== sender.frameId
  ) {
    return { ok: false, error: 'not_active', message: 'Wrong reading frame.' };
  }

  rememberState(state, false);

  if (state.status === 'done' || state.status === 'error') {
    await clearBadge(sender.tab.id);
    activeRead = null;
    persistActive();
  }

  forwardHud(sender.tab.id, state);
  return { ok: true };
}

async function onReadGone(sender) {
  if (
    !activeRead ||
    !sender.tab ||
    activeRead.tabId !== sender.tab.id ||
    activeRead.frameId !== sender.frameId
  ) {
    return { ok: true };
  }
  await stopActive({ reason: 'navigation', notify: true });
  return { ok: true };
}

async function onHudControl(message, sender) {
  if (!sender.tab || sender.frameId !== 0) {
    return { ok: false, error: 'bad_request', message: 'HUD must be top frame.' };
  }
  if (!activeRead || activeRead.tabId !== sender.tab.id) {
    return { ok: false, error: 'not_active', message: 'Nothing is reading.' };
  }

  var action = message.action;
  if (action === 'stop' || action === 'close') {
    await stopActive({ reason: 'hud-' + action, notify: true });
    return { ok: true };
  }
  if (action !== 'pause' && action !== 'resume') {
    return { ok: false, error: 'bad_request', message: 'Unknown HUD action.' };
  }

  var response = await sendToFrame(activeRead.tabId, activeRead.frameId, {
    type: 'HUD_CONTROL',
    action: action
  });
  if (!response) {
    return {
      ok: false,
      error: 'no_content',
      message: 'The reading frame is no longer available.'
    };
  }
  return { ok: true };
}

async function getReadState() {
  return {
    ok: true,
    active: !!activeRead,
    state: lastState || null
  };
}

async function testBridge(message) {
  var settings = await getSettings({
    bridgeUrl:
      typeof message.bridgeUrl === 'string' ? message.bridgeUrl : undefined,
    bridgeToken:
      typeof message.bridgeToken === 'string'
        ? message.bridgeToken
        : undefined
  });
  await getHealth(settings);
  var info = await getInfo(settings);
  return { ok: true, info: info };
}

async function handleMessage(message, sender) {
  if (!message || typeof message !== 'object' || typeof message.type !== 'string') {
    return {
      ok: false,
      error: 'bad_message',
      message: 'Malformed extension message.'
    };
  }

  /* Never dispatch against an empty globals snapshot while restore is pending. */
  await restoreSession();

  switch (message.type) {
    case 'SYNTHESIZE':
      return synthesize(message, sender);
    case 'BRIDGE_INFO': {
      if (!sender.tab) {
        return {
          ok: false,
          error: 'bad_request',
          message: 'Bridge info must come from a page.'
        };
      }
      var settings = await getSettings();
      var info = await getInfo(settings);
      return { ok: true, info: info };
    }
    case 'READ_STARTED':
      return onReadStarted(message, sender);
    case 'READ_STATE':
      return onReadState(message, sender);
    case 'READ_GONE':
      return onReadGone(sender);
    case 'HUD_CONTROL':
      return onHudControl(message, sender);
    case 'GET_READ_STATE':
      return getReadState();
    case 'START_READ': {
      /* Content scripts start in their own frame; popups use the active tab. */
      if (sender.tab) {
        return startRead(sender.tab.id, sender.frameId || 0, {
          fallbackText:
            typeof message.text === 'string' ? message.text : undefined,
          source: 'content'
        });
      }
      return startFromActiveTab();
    }
    case 'STOP_READ': {
      if (sender.tab) {
        if (
          !activeRead ||
          activeRead.tabId !== sender.tab.id ||
          activeRead.frameId !== sender.frameId
        ) {
          return { ok: true };
        }
        return stopActive({ reason: 'content-request', notify: true });
      }
      return stopActive({ reason: 'request', notify: true });
    }
    case 'TEST_BRIDGE':
      return testBridge(message);
    case 'OPEN_OPTIONS':
      try {
        chrome.runtime.openOptionsPage();
      } catch (err) {
        /* ignore */
      }
      return { ok: true };
    default:
      return {
        ok: false,
        error: 'bad_message',
        message: 'Unknown extension message type.'
      };
  }
}

chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
  handleMessage(message, sender).then(sendResponse, function (err) {
    sendResponse(failure(err));
  });
  return true;
});

/* ------------------------------------------------------------------ */
/* Context menus, commands, navigation                                 */
/* ------------------------------------------------------------------ */

var menusPromise = null;

function createMenus() {
  if (menusPromise) return menusPromise;
  menusPromise = new Promise(function (resolve) {
    try {
      chrome.contextMenus.removeAll(function () {
        chrome.contextMenus.create({
          id: MENU_READ,
          title: 'Read selection aloud',
          contexts: ['selection']
        });
        chrome.contextMenus.create({
          id: MENU_STOP,
          title: 'Stop reading',
          contexts: ['page', 'selection']
        });
        resolve();
      });
    } catch (err) {
      resolve();
    }
  });
  return menusPromise;
}

chrome.contextMenus.onClicked.addListener(function (info, tab) {
  if (!tab || typeof tab.id !== 'number') return;
  if (info.menuItemId === MENU_READ) {
    startRead(tab.id, typeof info.frameId === 'number' ? info.frameId : 0, {
      fallbackText:
        typeof info.selectionText === 'string' ? info.selectionText : '',
      source: 'context-menu'
    });
  } else if (info.menuItemId === MENU_STOP) {
    stopActive({ reason: 'context-menu', notify: true });
  }
});

chrome.commands.onCommand.addListener(async function (command) {
  if (command === 'stop-reading') {
    await stopActive({ reason: 'command', notify: true });
  } else if (command === 'read-selection') {
    await startFromActiveTab();
  }
});

function sameDocumentIgnoringFragment(before, after) {
  try {
    var first = new URL(before);
    var second = new URL(after);
    first.hash = '';
    second.hash = '';
    return first.href === second.href;
  } catch (err) {
    return false;
  }
}

chrome.tabs.onUpdated.addListener(function (tabId, changeInfo) {
  restoreSession().then(function () {
    if (!activeRead || activeRead.tabId !== tabId) return;
    if (
      typeof changeInfo.url === 'string' &&
      activeRead.url &&
      sameDocumentIgnoringFragment(activeRead.url, changeInfo.url)
    ) {
      /* A fragment-only jump is still the same document. */
      return;
    }
    if (changeInfo.status === 'loading' || typeof changeInfo.url === 'string') {
      stopActive({ reason: 'navigation', notify: false });
    }
  });
});

chrome.tabs.onRemoved.addListener(function (tabId) {
  restoreSession().then(function () {
    if (activeRead && activeRead.tabId === tabId) {
      stopActive({ reason: 'tab-closed', notify: false });
    }
  });
});

chrome.runtime.onInstalled.addListener(function () {
  ensureDefaults();
  createMenus();
});

chrome.runtime.onStartup.addListener(function () {
  ensureDefaults();
  createMenus();
  restoreSession();
});

/* Boot the worker. */
ensureDefaults();
createMenus();
restoreSession();
