/*
 * Omarchy TTS Reader - shared constants and pure helpers.
 *
 * Classic script. The service worker loads it with importScripts(), and every
 * extension page / content script loads it before its own script. It must not
 * touch chrome.* APIs or the DOM.
 */
(function (root) {
  'use strict';

  var DEFAULTS = Object.freeze({
    bridgeUrl: 'http://127.0.0.1:8787',
    bridgeToken: ''
  });

  var API = Object.freeze({
    health: '/v1/health',
    info: '/v1/info',
    synthesize: '/v1/synthesize',
    stop: '/v1/stop'
  });

  var TIMEOUTS = Object.freeze({
    infoMs: 5000,
    synthesizeMs: 130000,
    stopMs: 5000
  });

  var MAX_SELECTION_CHARS = 100000;
  var MAX_CHUNK_CHARS = 400;
  var MAX_SYNTH_CHARS = 20000;
  var MAX_AUDIO_BYTES = 16 * 1024 * 1024;

  var STATUSES = Object.freeze([
    'preparing',
    'speaking',
    'paused',
    'done',
    'error'
  ]);

  var AUDIO_MIME = Object.freeze({
    'audio/wav': 'wav',
    'audio/mpeg': 'mp3',
    'audio/ogg': 'ogg'
  });

  function safeText(value, maxLength) {
    if (typeof value !== 'string') return '';
    var limit = typeof maxLength === 'number' && maxLength > 0 ? maxLength : 200;
    return value.length > limit ? value.slice(0, limit) : value;
  }

  function trimMessage(value, maxLength) {
    var text = safeText(value, maxLength || 800);
    return text.replace(/\s+/g, ' ').trim();
  }

  function isFiniteNumber(value) {
    return typeof value === 'number' && isFinite(value);
  }

  function clampNumber(value, min, max) {
    if (!isFiniteNumber(value)) return null;
    if (value < min) return min;
    if (value > max) return max;
    return value;
  }

  function clampInteger(value, min, max) {
    if (!isFiniteNumber(value)) return null;
    var n = Math.floor(value);
    if (n < min) return min;
    if (n > max) return max;
    return n;
  }

  /*
   * Accept only the local bridge. The manifest grants host permission for
   * http://127.0.0.1/*, and the contract binds the bridge there.
   */
  function parseBridgeUrl(value) {
    if (typeof value !== 'string') return null;
    var raw = value.trim();
    if (!raw) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) raw = 'http://' + raw;

    var url;
    try {
      url = new URL(raw);
    } catch (err) {
      return null;
    }

    if (url.protocol !== 'http:') return null;
    if (url.hostname !== '127.0.0.1') return null;
    if (url.username || url.password) return null;
    if (url.search || url.hash) return null;
    if (url.pathname !== '/' && url.pathname !== '') return null;
    if (!url.port) return null;

    var port = Number(url.port);
    if (!isFinite(port) || port < 1 || port > 65535) return null;

    return url;
  }

  function bridgeBase(url) {
    return url.origin;
  }

  function normalizeBridgeUrl(value) {
    var url = parseBridgeUrl(value);
    return url ? bridgeBase(url) : '';
  }

  function formatRate(rate) {
    if (!isFiniteNumber(rate) || rate <= 0) return '';
    var text = String(Math.round(rate * 100) / 100);
    return text + '\u00d7';
  }

  function makeBridgeError(code, message, status) {
    var err = new Error(message || code || 'Bridge error');
    err.name = 'BridgeError';
    err.code = typeof code === 'string' && code ? code : 'bridge_error';
    if (isFiniteNumber(status)) err.status = status;
    return err;
  }

  function isBridgeError(err) {
    return !!(err && err.name === 'BridgeError' && typeof err.code === 'string');
  }

  function parseErrorBody(body) {
    var result = { error: '', message: '' };
    if (typeof body !== 'string' || !body.trim()) return result;

    var data = null;
    try {
      data = JSON.parse(body);
    } catch (err) {
      data = null;
    }

    if (data && typeof data === 'object') {
      var code = safeText(data.error, 48).toLowerCase();
      if (/^[a-z0-9_]+$/.test(code)) result.error = code;
      result.message = trimMessage(data.message, 600);
    }
    return result;
  }

  function mimeForFormat(format) {
    var value = safeText(format, 20).toLowerCase();
    if (value === 'wav') return 'audio/wav';
    if (value === 'mp3') return 'audio/mpeg';
    if (value === 'ogg') return 'audio/ogg';
    return '';
  }

  function bytesToBase64(bytes) {
    if (!(bytes instanceof Uint8Array) || bytes.length === 0) return '';
    var chunkSize = 0x8000;
    var chunks = [];
    for (var i = 0; i < bytes.length; i += chunkSize) {
      chunks.push(
        String.fromCharCode.apply(
          null,
          bytes.subarray(i, Math.min(i + chunkSize, bytes.length))
        )
      );
    }
    return root.btoa(chunks.join(''));
  }

  function base64ToBytes(value) {
    if (typeof value !== 'string' || !value) return null;
    var binary;
    try {
      binary = root.atob(value);
    } catch (err) {
      return null;
    }
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  var exported = {
    DEFAULTS: DEFAULTS,
    API: API,
    TIMEOUTS: TIMEOUTS,
    MAX_SELECTION_CHARS: MAX_SELECTION_CHARS,
    MAX_CHUNK_CHARS: MAX_CHUNK_CHARS,
    MAX_SYNTH_CHARS: MAX_SYNTH_CHARS,
    MAX_AUDIO_BYTES: MAX_AUDIO_BYTES,
    STATUSES: STATUSES,
    AUDIO_MIME: AUDIO_MIME,
    safeText: safeText,
    trimMessage: trimMessage,
    isFiniteNumber: isFiniteNumber,
    clampNumber: clampNumber,
    clampInteger: clampInteger,
    parseBridgeUrl: parseBridgeUrl,
    bridgeBase: bridgeBase,
    normalizeBridgeUrl: normalizeBridgeUrl,
    formatRate: formatRate,
    makeBridgeError: makeBridgeError,
    isBridgeError: isBridgeError,
    parseErrorBody: parseErrorBody,
    mimeForFormat: mimeForFormat,
    bytesToBase64: bytesToBase64,
    base64ToBytes: base64ToBytes
  };

  root.OmarchyTTS = Object.freeze(exported);
})(typeof self !== 'undefined' ? self : globalThis);
