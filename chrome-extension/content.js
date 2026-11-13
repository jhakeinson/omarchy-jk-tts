/*
 * Omarchy TTS Reader - content script.
 *
 * Kept as a classic script (no modules). Shared constants come from shared.js.
 *
 * The text helpers are exposed as a plain local object (not window) so they
 * stay testable in isolation while production code has no test hooks.
 */
'use strict';

const OmarchyTtsText = (function () {
  function isWhitespace(ch) {
    return /\s/.test(ch);
  }

  function trimRange(text, start, end) {
    while (start < end && isWhitespace(text.charAt(start))) start += 1;
    while (end > start && isWhitespace(text.charAt(end - 1))) end -= 1;
    return { start: start, end: end };
  }

  /*
   * Intersect one text node with a Range. Returns {start, end} character
   * offsets inside the node, or null when the intersection is empty.
   */
  function intersectTextNode(range, node) {
    if (!node || node.nodeType !== 3 || !node.data) return null;
    var nodeRange = node.ownerDocument.createRange();
    nodeRange.selectNodeContents(node);

    try {
      /*
       * START_TO_END compares this range's end with the node's start, and
       * END_TO_START compares this range's start with the node's end.
       */
      if (range.compareBoundaryPoints(Range.START_TO_END, nodeRange) <= 0) {
        return null;
      }
      if (range.compareBoundaryPoints(Range.END_TO_START, nodeRange) >= 0) {
        return null;
      }
    } catch (err) {
      return null;
    }

    var start = 0;
    var end = node.data.length;
    if (range.startContainer === node) start = range.startOffset;
    if (range.endContainer === node) end = range.endOffset;
    start = Math.max(0, Math.min(start, node.data.length));
    end = Math.max(start, Math.min(end, node.data.length));
    if (end <= start) return null;
    return { start: start, end: end };
  }

  /*
   * Return the nearest block-level ancestor for a text node, so separate
   * paragraphs can be joined with a space while inline markup is not.
   */
  function blockAncestor(node) {
    var element = node && node.parentElement;
    while (element) {
      if (
        /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|DD|DIV|DL|DT|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|TABLE|TBODY|TD|TFOOT|TH|THEAD|TR|UL)$/.test(
          element.tagName
        )
      ) {
        return element;
      }
      element = element.parentElement;
    }
    return null;
  }

  /*
   * Chrome's visible selection skips text that is not being rendered, and a
   * DOM Range still intersects it. Inline <script>/<style> data in particular
   * is full of base64 payloads that the TTS engine would otherwise read
   * aloud, so only text nodes with a rendered ancestor may join the map.
   */
  var UNRENDERED_TAGS = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD)$/i;

  function isRenderedTextNode(node, cache) {
    var doc = node.ownerDocument;
    var element = node.parentElement;
    while (element && element !== doc.documentElement) {
      /* SVG (and XML documents) lower-case tagName; match case-insensitively. */
      if (UNRENDERED_TAGS.test(element.tagName)) return false;
      if (cache) {
        if (cache.has(element)) {
          if (!cache.get(element)) return false;
        } else {
          var rendered = true;
          try {
            var view = doc.defaultView;
            var style = view && view.getComputedStyle(element);
            if (style) {
              /*
               * Selected text can intersect content the user cannot see.
               * Skip hidden content, not just display:none, so payloads in
               * collapsed/visibility-hidden containers are not spoken.
               */
              var contentVisibility =
                style.contentVisibility || style.webkitContentVisibility;
              rendered =
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.visibility !== 'collapse' &&
                contentVisibility !== 'hidden';
            }
          } catch (err) {
            /* Detached documents have no style; keep the node. */
          }
          cache.set(element, rendered);
          if (!rendered) return false;
        }
      }
      element = element.parentElement;
    }
    return true;
  }

  /*
   * Walk the document text nodes covered by `range`, producing canonical text
   * plus a map from canonical offsets back to text nodes.
   *
   * Text in separate block elements is joined with one space so a selection
   * spanning paragraphs does not produce "page.It". Entries are
   * { node, nodeStart, nodeEnd, textStart, textEnd, block }.
   */
  function buildTextMap(range, options) {
    var opts = options || {};
    var maxChars =
      typeof opts.maxChars === 'number' && opts.maxChars > 0
        ? opts.maxChars
        : 100000;
    var doc = range.startContainer.ownerDocument;
    var rootNode = doc.body || doc.documentElement;
    var walker = doc.createTreeWalker(rootNode, NodeFilter.SHOW_TEXT, null);
    var entries = [];
    var text = '';
    var truncated = false;
    var rendered = new Map();
    var node = walker.nextNode();

    while (node) {
      if (isRenderedTextNode(node, rendered)) {
        var hit = intersectTextNode(range, node);
        if (hit) {
          var piece = node.data.slice(hit.start, hit.end);
          var block = blockAncestor(node);
          var previous = entries.length ? entries[entries.length - 1] : null;
          var separator =
            previous &&
            previous.block !== block &&
            !/\s$/.test(text) &&
            !/^\s/.test(piece)
              ? ' '
              : '';
          var takeEnd = hit.end;
          if (
            text.length + separator.length + (takeEnd - hit.start) >
            maxChars
          ) {
            takeEnd =
              hit.start + (maxChars - text.length - separator.length);
            truncated = true;
          }
          if (takeEnd > hit.start) {
            piece = node.data.slice(hit.start, takeEnd);
            text += separator;
            entries.push({
              node: node,
              nodeStart: hit.start,
              nodeEnd: takeEnd,
              textStart: text.length,
              textEnd: text.length + piece.length,
              block: block
            });
            text += piece;
          }
          if (truncated || text.length >= maxChars) {
            truncated = true;
            break;
          }
        }
      }
      node = walker.nextNode();
    }

    return { text: text, entries: entries, truncated: truncated };
  }

  /*
   * Build a live Range from canonical text offsets. Returns null when the map
   * cannot represent the requested span.
   */
  function rangeForOffsets(map, start, end) {
    if (!map || !map.entries || !map.entries.length) return null;
    var from = Math.max(0, Math.min(start, map.text.length));
    var to = Math.max(from, Math.min(end, map.text.length));
    var startNode = null;
    var startOffset = 0;
    var endNode = null;
    var endOffset = 0;

    for (var i = 0; i < map.entries.length; i += 1) {
      var entry = map.entries[i];
      if (!startNode && entry.textEnd > from) {
        startNode = entry.node;
        startOffset = entry.nodeStart + (from - entry.textStart);
      }
      if (entry.textStart >= to && endNode) break;
      if (entry.textStart < to && entry.textEnd > from) {
        endNode = entry.node;
        endOffset =
          entry.nodeStart + (Math.min(to, entry.textEnd) - entry.textStart);
      }
    }

    if (!startNode || !endNode) return null;
    try {
      var range = startNode.ownerDocument.createRange();
      range.setStart(startNode, startOffset);
      range.setEnd(endNode, endOffset);
      return range;
    } catch (err) {
      return null;
    }
  }

  function pushChunk(out, text, start, end, maxChars) {
    var trimmed = trimRange(text, start, end);
    if (trimmed.end <= trimmed.start) return;
    if (trimmed.end - trimmed.start <= maxChars) {
      out.push({
        text: text.slice(trimmed.start, trimmed.end),
        start: trimmed.start,
        end: trimmed.end
      });
      return;
    }

    var cursor = trimmed.start;
    while (cursor < trimmed.end) {
      if (trimmed.end - cursor <= maxChars) {
        var tail = trimRange(text, cursor, trimmed.end);
        if (tail.end > tail.start) {
          out.push({
            text: text.slice(tail.start, tail.end),
            start: tail.start,
            end: tail.end
          });
        }
        return;
      }

      var limit = cursor + maxChars;
      var cut = -1;
      for (var p = limit; p > cursor + Math.floor(maxChars * 0.5); p -= 1) {
        if (isWhitespace(text.charAt(p))) {
          cut = p;
          break;
        }
      }
      if (cut <= cursor) cut = limit;

      var piece = trimRange(text, cursor, cut);
      if (piece.end > piece.start) {
        out.push({
          text: text.slice(piece.start, piece.end),
          start: piece.start,
          end: piece.end
        });
      }
      cursor = cut;
    }
  }

  /*
   * Split canonical text into sentence-ish chunks of at most maxChars.
   * Uses Intl.Segmenter when available and a punctuation regex otherwise.
   * Returns [{ text, start, end }] with offsets into the input text.
   */
  function splitIntoChunks(text, options) {
    var opts = options || {};
    var maxChars =
      typeof opts.maxChars === 'number' && opts.maxChars > 0
        ? opts.maxChars
        : 400;
    var out = [];
    if (typeof text !== 'string' || !text.trim()) return out;

    var segments = [];
    try {
      if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
        var segmenter = new Intl.Segmenter(undefined, {
          granularity: 'sentence'
        });
        var iterator = segmenter.segment(text);
        for (var item of iterator) {
          segments.push({
            start: item.index,
            end: item.index + item.segment.length
          });
        }
      }
    } catch (err) {
      segments = [];
    }

    if (!segments.length) {
      var re = /[^.!?\u2026]+[.!?\u2026]+["'\u201d\u2019)\]]*|[^.!?\u2026]+$/g;
      var match = re.exec(text);
      while (match) {
        if (match[0].length === 0) {
          re.lastIndex += 1;
        } else {
          segments.push({
            start: match.index,
            end: match.index + match[0].length
          });
        }
        match = re.exec(text);
      }
    }

    if (!segments.length) {
      segments.push({ start: 0, end: text.length });
    }

    for (var i = 0; i < segments.length; i += 1) {
      pushChunk(out, text, segments[i].start, segments[i].end, maxChars);
    }
    return out;
  }

  return Object.freeze({
    buildTextMap: buildTextMap,
    rangeForOffsets: rangeForOffsets,
    splitIntoChunks: splitIntoChunks,
    trimRange: trimRange,
    intersectTextNode: intersectTextNode
  });
})();

(function () {
  if (window.__omarchyTtsContentLoaded) return;
  window.__omarchyTtsContentLoaded = true;

  var T = self.OmarchyTTS;
  var isTopFrame = (function () {
    try {
      return window.top === window;
    } catch (err) {
      return false;
    }
  })();

  var session = null;
  var generation = 0;
  var hud = null;
  var hudHideTimer = null;

  /* ---------------------------------------------------------------- */
  /* Messaging                                                         */
  /* ---------------------------------------------------------------- */

  function notifyBackground(message) {
    try {
      chrome.runtime.sendMessage(message, function () {
        /* Reading the property clears "lastError" for context loss. */
        void chrome.runtime.lastError;
      });
    } catch (err) {
      /* Extension was reloaded; nothing useful to do from the page. */
    }
  }

  function requestBackground(message, timeoutMs) {
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;

      function finish(value) {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve(value === undefined ? null : value);
      }

      try {
        chrome.runtime.sendMessage(message, function (response) {
          void chrome.runtime.lastError;
          finish(response);
        });
      } catch (err) {
        finish(null);
        return;
      }

      if (timeoutMs) {
        timer = setTimeout(function () {
          finish(null);
        }, timeoutMs);
      }
    });
  }

  function makeContentError(code, message) {
    var err = new Error(message || 'The bridge request failed.');
    err.code = code || 'bridge_error';
    return err;
  }

  /* ---------------------------------------------------------------- */
  /* Session and highlights                                            */
  /* ---------------------------------------------------------------- */

  function supportsHighlights() {
    return (
      typeof CSS !== 'undefined' &&
      CSS.highlights &&
      typeof Highlight === 'function'
    );
  }

  function clearHighlights() {
    if (!supportsHighlights()) return;
    try {
      CSS.highlights.delete('omarchy-tts-scope');
      CSS.highlights.delete('omarchy-tts-current');
    } catch (err) {
      /* ignore */
    }
  }

  function setScopeHighlight(map, textLength) {
    if (!supportsHighlights() || !map) return;
    var range = OmarchyTtsText.rangeForOffsets(map, 0, textLength);
    if (!range) return;
    try {
      CSS.highlights.set('omarchy-tts-scope', new Highlight(range));
    } catch (err) {
      /* Highlight API can reject detached ranges. */
    }
  }

  function setCurrentHighlight(map, chunk) {
    if (!supportsHighlights() || !map || !chunk) return;
    var range = OmarchyTtsText.rangeForOffsets(map, chunk.start, chunk.end);
    if (!range) return;
    try {
      CSS.highlights.set('omarchy-tts-current', new Highlight(range));
      return range;
    } catch (err) {
      return null;
    }
  }

  function clearCurrentHighlight() {
    if (!supportsHighlights()) return;
    try {
      CSS.highlights.delete('omarchy-tts-current');
    } catch (err) {
      /* ignore */
    }
  }

  function releaseAudio() {
    if (!session) return;
    var audio = session.audio;
    session.audio = null;
    if (audio) {
      try {
        audio.pause();
        audio.removeAttribute('src');
      } catch (err) {
        /* ignore */
      }
    }
    if (session.objectUrl) {
      try {
        URL.revokeObjectURL(session.objectUrl);
      } catch (err) {
        /* ignore */
      }
      session.objectUrl = null;
    }
  }

  function stopTicker() {
    if (session && session.ticker) {
      clearInterval(session.ticker);
      session.ticker = null;
    }
  }

  function resetSession() {
    stopTicker();
    if (session) {
      releaseAudio();
      session.cache.clear();
    }
    clearHighlights();
    session = null;
  }

  function isCurrent(gen) {
    return !!(session && session.generation === gen);
  }

  /* ---------------------------------------------------------------- */
  /* Selection capture                                                 */
  /* ---------------------------------------------------------------- */

  function captureTextField() {
    var el = document.activeElement;
    if (!el) return null;
    var isTextField =
      el.tagName === 'TEXTAREA' ||
      (el.tagName === 'INPUT' &&
        /^(text|search|url|tel|email)$/i.test(el.type || 'text'));
    if (!isTextField) return null;
    /* Never read secrets (passwords, one-time codes, payment data) aloud. */
    var autocomplete = String(el.autocomplete || '').toLowerCase();
    if (
      /(^|\s)(current-password|new-password|one-time-code|cc-\w+)/.test(
        autocomplete
      )
    ) {
      return null;
    }

    var start = el.selectionStart;
    var end = el.selectionEnd;
    if (typeof start !== 'number' || typeof end !== 'number' || end <= start) {
      return null;
    }
    var text = el.value.slice(start, end);
    if (!text.trim()) return null;
    var truncated = text.length > T.MAX_SELECTION_CHARS;
    if (truncated) text = text.slice(0, T.MAX_SELECTION_CHARS);
    return {
      kind: 'input',
      text: text,
      map: null,
      range: null,
      truncated: truncated
    };
  }

  function captureSelection(fallbackText) {
    try {
      var selection = window.getSelection();
      if (selection && selection.rangeCount > 0 && !selection.isCollapsed) {
        var range = selection.getRangeAt(0).cloneRange();
        var map = OmarchyTtsText.buildTextMap(range, {
          maxChars: T.MAX_SELECTION_CHARS
        });
        if (map.text.trim()) {
          return {
            kind: 'dom',
            text: map.text,
            map: map,
            range: range,
            truncated: map.truncated
          };
        }
      }
    } catch (err) {
      /* Fall through to inputs / caller-supplied text. */
    }

    var input = captureTextField();
    if (input) return input;

    if (typeof fallbackText === 'string' && fallbackText.trim()) {
      var text = fallbackText.trim();
      var truncated = text.length > T.MAX_SELECTION_CHARS;
      if (truncated) text = text.slice(0, T.MAX_SELECTION_CHARS);
      return {
        kind: 'plain',
        text: text,
        map: null,
        range: null,
        truncated: truncated
      };
    }
    return null;
  }

  /* ---------------------------------------------------------------- */
  /* Scrolling                                                         */
  /* ---------------------------------------------------------------- */

  function prefersReducedMotion() {
    try {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (err) {
      return false;
    }
  }

  /*
   * Put a temporary zero-size marker at the start of the current range and
   * scroll it into the middle of the viewport. Inserting the marker splits a
   * text node, so the map is rebuilt lazily before the next highlight.
   */
  function scrollRangeIntoView(range, gen) {
    if (!range) return;
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    var rect = null;
    try {
      rect = range.getBoundingClientRect();
    } catch (err) {
      rect = null;
    }

    var inBand =
      rect &&
      rect.height > 0 &&
      rect.top >= vh * 0.2 &&
      rect.bottom <= vh * 0.8;
    if (inBand) return;

    var marker = null;
    try {
      marker = document.createElement('span');
      marker.setAttribute('data-omarchy-tts-marker', '');
      marker.setAttribute('aria-hidden', 'true');
      marker.style.cssText =
        'all:initial;position:absolute;width:0;height:0;padding:0;margin:0;' +
        'border:0;overflow:hidden;pointer-events:none;';
      var collapsed = range.cloneRange();
      collapsed.collapse(true);
      collapsed.insertNode(marker);
      if (isCurrent(gen) && session) session.mapDirty = true;
      marker.scrollIntoView({
        block: 'center',
        inline: 'nearest',
        behavior: prefersReducedMotion() ? 'auto' : 'smooth'
      });
    } catch (err) {
      /* Detached ranges, document boundaries, and cross-document frames. */
    } finally {
      if (marker) {
        try {
          marker.remove();
        } catch (err) {
          /* ignore */
        }
      }
    }
  }

  /* ---------------------------------------------------------------- */
  /* Placeholder for playback and HUD (filled in below).               */
  /* ---------------------------------------------------------------- */

  /* ---------------------------------------------------------------- */
  /* State reporting                                                   */
  /* ---------------------------------------------------------------- */

  function buildState(status, extra) {
    var chunk =
      session && session.index >= 0 ? session.chunks[session.index] : null;
    var state = {
      status: status,
      index: session ? Math.max(0, session.index + 1) : 0,
      total: session ? session.chunks.length : 0,
      text: chunk ? chunk.text : '',
      provider: session ? session.provider : null,
      voice: session ? session.voice : null,
      rate: session ? session.rate : null
    };
    if (extra && typeof extra === 'object') {
      var keys = Object.keys(extra);
      for (var i = 0; i < keys.length; i += 1) {
        state[keys[i]] = extra[keys[i]];
      }
    }
    return state;
  }

  function sendState(state) {
    notifyBackground({ type: 'READ_STATE', state: state });
  }

  function sendCurrentState(status, extra) {
    sendState(buildState(status, extra));
  }

  function updateMetadata(result) {
    if (!session || !result) return;
    if (typeof result.provider === 'string' && result.provider) {
      session.provider = result.provider;
    }
    if (typeof result.voice === 'string' && result.voice) {
      session.voice = result.voice;
    }
    if (T.isFiniteNumber(result.rate)) session.rate = result.rate;
  }

  async function requestInfo(gen) {
    var response = await requestBackground({ type: 'BRIDGE_INFO' }, 1800);
    if (!isCurrent(gen) || !session) return null;
    if (!response || response.ok !== true || !response.info) return null;
    var info = response.info;
    if (!session.provider) session.provider = T.safeText(info.provider, 120);
    if (!session.voice) session.voice = T.safeText(info.voice, 160);
    if (session.rate === null && T.isFiniteNumber(info.rate)) {
      session.rate = info.rate;
    }
    return info;
  }

  /* ---------------------------------------------------------------- */
  /* Highlight + scroll for one chunk                                  */
  /* ---------------------------------------------------------------- */

  function refreshMapIfDirty() {
    if (!session || !session.map || !session.mapDirty) return;
    session.mapDirty = false;
    if (!session.savedRange) {
      session.map = null;
      return;
    }
    try {
      var rebuilt = OmarchyTtsText.buildTextMap(session.savedRange, {
        maxChars: T.MAX_SELECTION_CHARS
      });
      if (rebuilt && rebuilt.text === session.text) {
        session.map = rebuilt;
      } else {
        /* The page changed under us; keep reading, stop highlighting. */
        session.map = null;
      }
    } catch (err) {
      session.map = null;
    }
  }

  function showChunk(i, gen) {
    if (!isCurrent(gen) || !session || session.kind !== 'dom') return;
    var chunk = session.chunks[i];
    if (!chunk) return;
    refreshMapIfDirty();
    if (!session.map) return;
    var range = setCurrentHighlight(session.map, chunk);
    if (range) scrollRangeIntoView(range, gen);
  }

  /* ---------------------------------------------------------------- */
  /* Synthesis and playback                                            */
  /* ---------------------------------------------------------------- */

  function synthesizeChunk(i, gen) {
    if (!session || i < 0 || i >= session.chunks.length) {
      return Promise.reject(
        makeContentError('bad_request', 'No such sentence to synthesize.')
      );
    }
    if (session.cache.has(i)) return session.cache.get(i);

    var chunk = session.chunks[i];
    var promise = requestBackground(
      { type: 'SYNTHESIZE', text: chunk.text },
      T.TIMEOUTS.synthesizeMs + 10000
    ).then(function (response) {
      if (!response) {
        throw makeContentError(
          'no_response',
          'The extension stopped responding. Reload the page and try again.'
        );
      }
      if (response.ok !== true) {
        throw makeContentError(
          response.error || 'bridge_error',
          T.trimMessage(response.message, 800) ||
            'The TTS bridge could not synthesize this sentence.'
        );
      }
      return response;
    });

    /* A prefetched failure must not surface as an unhandled rejection. */
    promise.catch(function () {});
    session.cache.set(i, promise);

    var keys = Array.from(session.cache.keys());
    for (var k = 0; k < keys.length; k += 1) {
      if (keys[k] < i - 1) session.cache.delete(keys[k]);
    }
    return promise;
  }

  function prefetchChunk(i, gen) {
    if (!session || i >= session.chunks.length) return;
    synthesizeChunk(i, gen).catch(function () {
      /* The error is shown if/when playback reaches this chunk. */
    });
  }

  function audioProgress() {
    if (!session || !session.audio) return 0;
    var audio = session.audio;
    if (!isFinite(audio.duration) || audio.duration <= 0) return 0;
    return Math.max(0, Math.min(1, audio.currentTime / audio.duration));
  }

  function startTicker(gen) {
    stopTicker();
    if (!session) return;
    session.ticker = setInterval(function () {
      if (!isCurrent(gen) || !session) return;
      if (session.status !== 'speaking' || !session.audio) return;
      sendCurrentState('speaking', { progress: audioProgress() });
    }, 250);
  }

  function failReading(err) {
    if (!session) return;
    var message =
      T.trimMessage(err && err.message, 800) ||
      'The TTS bridge could not synthesize this selection.';
    var state = buildState('error', { message: message });
    stopTicker();
    resetSession();
    sendState(state);
  }

  function finishReading(extra) {
    if (!session) return;
    var state = buildState('done', Object.assign({ progress: 1 }, extra || {}));
    stopTicker();
    resetSession();
    sendState(state);
  }

  /*
   * Pause is a session-level intent, not just an audio element call. It also
   * works while the next chunk is still being synthesized or in the gap
   * between chunks: the walker parks at the pending sentence and continues
   * exactly there on resume.
   */
  function pauseReading() {
    if (!session || session.status === 'done' || session.status === 'error') {
      return;
    }
    session.paused = true;
    if (session.audio && session.status === 'speaking') {
      try {
        session.audio.pause();
      } catch (err) {
        /* ignore */
      }
    }
    if (session.status !== 'paused') {
      session.status = 'paused';
      stopTicker();
      sendCurrentState('paused', { progress: audioProgress() });
    }
  }

  function resumeReading() {
    if (!session || !session.paused) return;
    session.paused = false;
    var gen = session.generation;

    var audio = session.audio;
    if (audio) {
      /* Mid-sentence pause: the element continues from the same position. */
      audio.play().then(
        function () {
          if (!isCurrent(gen)) return;
          if (applyPauseIfWanted(gen)) return;
          session.status = 'speaking';
          var progress = 0;
          if (isFinite(audio.duration) && audio.duration > 0) {
            progress = Math.max(
              0,
              Math.min(1, audio.currentTime / audio.duration)
            );
          }
          sendCurrentState('speaking', { progress: progress });
          startTicker(gen);
        },
        function () {
          if (!isCurrent(gen)) return;
          session.paused = true;
          session.status = 'paused';
          sendCurrentState('paused', {
            message:
              'Audio is still blocked. Click Resume again, or allow audio for this site.'
          });
        }
      );
      return;
    }

    /* No audio yet: the walker was parked before a chunk. Continue there. */
    var pending =
      typeof session.pendingIndex === 'number'
        ? session.pendingIndex
        : session.index + 1;
    session.pendingIndex = null;
    if (pending < 0 || pending >= session.chunks.length) {
      finishReading();
      return;
    }
    playIndex(pending, gen);
  }

  /*
   * A pause request can land while playback is still being started (the
   * play() promise has not settled yet). Honor it immediately instead of
   * letting the speaking state clobber it.
   */
  function applyPauseIfWanted(gen) {
    if (!isCurrent(gen) || !session || !session.paused) return false;
    if (session.audio) {
      try {
        session.audio.pause();
      } catch (err) {
        /* ignore */
      }
    }
    session.status = 'paused';
    sendCurrentState('paused', { progress: audioProgress() });
    return true;
  }

  function onChunkEnded(i, gen) {
    if (!isCurrent(gen)) return;
    releaseAudio();
    clearCurrentHighlight();
    if (i + 1 >= session.chunks.length) {
      finishReading();
    } else {
      playIndex(i + 1, gen);
    }
  }

  async function playIndex(i, gen) {
    if (!isCurrent(gen) || !session) return;
    if (i < 0 || i >= session.chunks.length) {
      finishReading();
      return;
    }

    var serial = (session.playSerial || 0) + 1;
    session.playSerial = serial;

    if (session.paused) {
      /* Park the walker here; resumeReading continues from this chunk.
       * Point the position and highlight at the pending sentence, so the
       * paused state always names the sentence playback will resume on. */
      session.pendingIndex = i;
      session.index = i;
      showChunk(i, gen);
      sendCurrentState('paused', { progress: 0 });
      return;
    }

    /* Record the sentence before awaiting synthesis. Pausing in that window
     * must park on this chunk, not skip it; the serial makes an older walker
     * bail if resume starts this chunk again while the first request is
     * still in flight. */
    session.pendingIndex = i;
    session.index = i;
    session.status = i === 0 ? 'preparing' : 'speaking';
    showChunk(i, gen);

    if (i > 0 && isCurrent(gen)) {
      sendCurrentState('speaking', { progress: 0 });
    }

    var result;
    try {
      result = await synthesizeChunk(i, gen);
    } catch (err) {
      if (isCurrent(gen) && session.playSerial === serial) failReading(err);
      return;
    }
    if (!isCurrent(gen) || !session || session.playSerial !== serial) return;

    if (session.paused) {
      /* Pause landed while this chunk was being synthesized. Keep it cached
       * so resume does not pay for a second synthesis. */
      session.pendingIndex = i;
      sendCurrentState('paused', { progress: 0 });
      return;
    }

    session.cache.delete(i);

    if (result.empty) {
      session.emptyRun = (session.emptyRun || 0) + 1;
      if (session.emptyRun >= 4) {
        failReading(
          makeContentError(
            'empty_audio',
            'The bridge produced no speakable audio for this selection.'
          )
        );
        return;
      }
      if (i + 1 >= session.chunks.length) {
        finishReading();
        return;
      }
      setTimeout(function () {
        if (isCurrent(gen)) playIndex(i + 1, gen);
      }, 40);
      return;
    }

    session.emptyRun = 0;
    updateMetadata(result);

    var bytes = T.base64ToBytes(result.b64);
    if (!bytes || !bytes.length) {
      failReading(
        makeContentError(
          'audio_error',
          'The bridge returned empty audio for this sentence.'
        )
      );
      return;
    }

    var objectUrl;
    try {
      objectUrl = URL.createObjectURL(
        new Blob([bytes], { type: result.mime || 'audio/wav' })
      );
    } catch (err) {
      failReading(
        makeContentError('audio_error', 'The audio could not be prepared.')
      );
      return;
    }

    session.objectUrl = objectUrl;
    var audio = new Audio();
    session.audio = audio;
    audio.preload = 'auto';
    audio.src = objectUrl;
    audio.addEventListener('ended', function () {
      onChunkEnded(i, gen);
    });
    audio.addEventListener('error', function () {
      if (!isCurrent(gen)) return;
      failReading(
        makeContentError(
          'audio_error',
          'The browser could not play the synthesized audio.'
        )
      );
    });

    try {
      await audio.play();
    } catch (err) {
      if (!isCurrent(gen) || session.playSerial !== serial) return;
      /* Keep the pause intent in sync with the reported state: the HUD's
       * Resume button is driven by `status` but resumeReading() requires
       * `paused`, so both must be set or the button is a no-op. */
      session.paused = true;
      session.pendingIndex = i;
      session.status = 'paused';
      sendCurrentState('paused', {
        message:
          'Audio playback was blocked by the browser. Click Resume to allow it.'
      });
      return;
    }

    if (!isCurrent(gen) || !session || session.playSerial !== serial) return;
    if (applyPauseIfWanted(gen)) return;
    session.pendingIndex = null;
    session.status = 'speaking';
    sendCurrentState('speaking', { progress: 0 });
    startTicker(gen);
    prefetchChunk(i + 1, gen);
  }

  /* ---------------------------------------------------------------- */
  /* Start / stop                                                      */
  /* ---------------------------------------------------------------- */

  async function startPipeline(gen, total, notes) {
    await requestInfo(gen);
    if (!isCurrent(gen) || !session) return;

    notifyBackground({
      type: 'READ_STARTED',
      total: total,
      paused: session.paused === true,
      provider: session.provider,
      voice: session.voice,
      rate: session.rate
    });

    sendState(
      buildState(session.paused ? 'paused' : 'preparing', {
        message: notes.length ? notes.join(' ') : undefined
      })
    );

    playIndex(0, gen);
  }

  async function startReading(message) {
    generation += 1;
    var gen = generation;
    resetSession();

    var fallback =
      message && typeof message.text === 'string' ? message.text : '';
    var captured = captureSelection(fallback);

    if (!captured) {
      sendState({
        status: 'error',
        index: 0,
        total: 0,
        text: '',
        provider: null,
        voice: null,
        rate: null,
        message: 'Select some text first, then try again.'
      });
      return {
        ok: false,
        error: 'no_selection',
        message: 'Select some text first.'
      };
    }

    var chunks = OmarchyTtsText.splitIntoChunks(captured.text, {
      maxChars: T.MAX_CHUNK_CHARS
    });
    if (!chunks.length) {
      sendState({
        status: 'error',
        index: 0,
        total: 0,
        text: '',
        provider: null,
        voice: null,
        rate: null,
        message: 'There is no speakable text in the selection.'
      });
      return {
        ok: false,
        error: 'empty_selection',
        message: 'There is no speakable text in the selection.'
      };
    }

    session = {
      generation: gen,
      kind: captured.kind,
      text: captured.text,
      chunks: chunks,
      index: -1,
      status: 'preparing',
      paused: false,
      pendingIndex: null,
      playSerial: 0,
      provider: null,
      voice: null,
      rate: null,
      map: captured.map,
      mapDirty: false,
      savedRange: captured.range,
      audio: null,
      objectUrl: null,
      ticker: null,
      cache: new Map(),
      emptyRun: 0
    };

    if (captured.kind === 'dom' && captured.map) {
      setScopeHighlight(captured.map, captured.text.length);
    }

    var notes = [];
    if (captured.truncated) {
      notes.push(
        'Selection truncated to ' +
          T.MAX_SELECTION_CHARS.toLocaleString() +
          ' characters.'
      );
    }
    if (captured.kind !== 'dom') {
      notes.push('Reading without in-page highlighting.');
    }

    /* Show the HUD immediately, then enrich it with bridge metadata. */
    sendState(buildState('preparing'));
    startPipeline(gen, chunks.length, notes).catch(function () {});
    return { ok: true, total: chunks.length };
  }

  function stopReadingInternal() {
    generation += 1;
    resetSession();
    if (isTopFrame) hideHud();
  }

  /* ---------------------------------------------------------------- */
  /* Top-frame HUD                                                     */
  /* ---------------------------------------------------------------- */

  var HUD_CSS = `
:host { all: initial; }
*, *::before, *::after { box-sizing: border-box; }
.panel {
  width: 360px;
  max-width: calc(100vw - 36px);
  padding: 12px 14px;
  border: 1px solid rgba(24, 24, 27, 0.14);
  border-radius: 14px;
  background: #ffffff;
  color: #18181b;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 13px;
  line-height: 1.45;
  color-scheme: light dark;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18), 0 2px 8px rgba(0, 0, 0, 0.08);
  pointer-events: auto;
}
.head { display: flex; align-items: center; gap: 8px; }
.brand {
  color: #c2410c;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: 0.02em;
}
.icon {
  margin-left: auto;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #71717a;
  font: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.icon:hover { background: rgba(24, 24, 27, 0.08); color: #18181b; }
.meta {
  margin-top: 2px;
  color: #71717a;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sentence {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  max-height: 4.35em;
  margin-top: 8px;
  overflow: hidden;
  font-size: 13.5px;
  overflow-wrap: anywhere;
}
.positionRow { display: flex; align-items: center; gap: 8px; margin-top: 9px; }
.position { color: #71717a; font-size: 11px; white-space: nowrap; }
.track {
  position: relative;
  flex: 1;
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(24, 24, 27, 0.12);
}
.fill {
  width: 0%;
  height: 100%;
  border-radius: inherit;
  background: #f97316;
  transition: width 0.18s linear;
}
.message {
  margin-top: 8px;
  padding: 7px 8px;
  border-radius: 8px;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.message.notice { background: rgba(249, 115, 22, 0.1); color: #9a3412; }
.message.error { background: rgba(220, 38, 38, 0.1); color: #b91c1c; }
.actions { display: flex; align-items: center; gap: 6px; margin-top: 10px; }
.btn {
  padding: 5px 10px;
  border: 1px solid rgba(24, 24, 27, 0.16);
  border-radius: 8px;
  background: #f4f4f5;
  color: #18181b;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.btn:hover { background: #e4e4e7; }
.btn.primary {
  border-color: #ea580c;
  background: #f97316;
  color: #ffffff;
}
.btn.primary:hover { background: #ea580c; }
.btn:disabled { opacity: 0.5; cursor: default; }
.link {
  margin-left: auto;
  padding: 4px 2px;
  border: 0;
  background: transparent;
  color: #c2410c;
  font: inherit;
  font-size: 12px;
  text-decoration: underline;
  cursor: pointer;
}
.btn:focus-visible, .icon:focus-visible, .link:focus-visible {
  outline: 2px solid #f97316;
  outline-offset: 2px;
}
@media (prefers-color-scheme: dark) {
  .panel {
    border-color: rgba(250, 250, 250, 0.16);
    background: #18181b;
    color: #fafafa;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.55);
  }
  .brand { color: #fb923c; }
  .meta, .position { color: #a1a1aa; }
  .icon { color: #a1a1aa; }
  .icon:hover { background: rgba(250, 250, 250, 0.1); color: #fafafa; }
  .track { background: rgba(250, 250, 250, 0.18); }
  .btn {
    border-color: rgba(250, 250, 250, 0.2);
    background: #27272a;
    color: #fafafa;
  }
  .btn:hover { background: #3f3f46; }
  .btn.primary { border-color: #ea580c; background: #f97316; color: #ffffff; }
  .link { color: #fb923c; }
  .message.notice { background: rgba(251, 146, 60, 0.16); color: #fdba74; }
  .message.error { background: rgba(248, 113, 113, 0.16); color: #fca5a5; }
}
@media (prefers-reduced-motion: reduce) {
  .fill { transition: none; }
}
`;

  function ensureHud() {
    if (!isTopFrame) return null;
    if (hud) {
      /* A page that rewrites <body> must not permanently lose the reader. */
      if (!hud.host.isConnected) {
        (document.body || document.documentElement).appendChild(hud.host);
      }
      return hud;
    }

    var host = document.createElement('div');
    host.id = 'omarchy-tts-hud';
    host.setAttribute('role', 'region');
    host.setAttribute('aria-label', 'Omarchy TTS reader');
    host.style.cssText =
      'all:initial !important;position:fixed !important;' +
      'right:18px !important;bottom:18px !important;' +
      'z-index:2147483647 !important;pointer-events:auto !important;' +
      'visibility:visible !important;opacity:1 !important;' +
      'display:none !important;';

    var root;
    try {
      root = host.attachShadow({ mode: 'closed' });
    } catch (err) {
      return null;
    }

    var style = document.createElement('style');
    style.textContent = HUD_CSS;
    root.appendChild(style);

    var panel = document.createElement('div');
    panel.className = 'panel';

    var head = document.createElement('div');
    head.className = 'head';
    var brand = document.createElement('div');
    brand.className = 'brand';
    brand.textContent = 'Omarchy TTS';
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'icon';
    closeBtn.setAttribute('aria-label', 'Close reader');
    closeBtn.title = 'Close';
    closeBtn.textContent = '\u00d7';
    head.appendChild(brand);
    head.appendChild(closeBtn);

    var meta = document.createElement('div');
    meta.className = 'meta';

    var sentence = document.createElement('div');
    sentence.className = 'sentence';
    sentence.setAttribute('aria-live', 'polite');

    var positionRow = document.createElement('div');
    positionRow.className = 'positionRow';
    var position = document.createElement('span');
    position.className = 'position';
    var track = document.createElement('div');
    track.className = 'track';
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', '100');
    track.setAttribute('aria-valuenow', '0');
    var fill = document.createElement('div');
    fill.className = 'fill';
    track.appendChild(fill);
    positionRow.appendChild(position);
    positionRow.appendChild(track);

    var message = document.createElement('div');
    message.className = 'message';
    message.hidden = true;

    var actions = document.createElement('div');
    actions.className = 'actions';
    var pauseBtn = document.createElement('button');
    pauseBtn.type = 'button';
    pauseBtn.className = 'btn primary';
    pauseBtn.textContent = 'Pause';
    var stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'btn';
    stopBtn.textContent = 'Stop';
    var settingsBtn = document.createElement('button');
    settingsBtn.type = 'button';
    settingsBtn.className = 'link';
    settingsBtn.textContent = 'Settings';
    actions.appendChild(pauseBtn);
    actions.appendChild(stopBtn);
    actions.appendChild(settingsBtn);

    panel.appendChild(head);
    panel.appendChild(meta);
    panel.appendChild(sentence);
    panel.appendChild(positionRow);
    panel.appendChild(message);
    panel.appendChild(actions);
    root.appendChild(panel);

    closeBtn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      hideHud();
      notifyBackground({ type: 'HUD_CONTROL', action: 'close' });
    });

    stopBtn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      hideHud();
      notifyBackground({ type: 'HUD_CONTROL', action: 'stop' });
    });

    pauseBtn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      var status = hud && hud.lastState ? hud.lastState.status : '';
      notifyBackground({
        type: 'HUD_CONTROL',
        action: status === 'paused' ? 'resume' : 'pause'
      });
    });

    settingsBtn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      notifyBackground({ type: 'OPEN_OPTIONS' });
    });

    /* Do not let page listeners observe HUD clicks. */
    panel.addEventListener('click', function (event) {
      event.stopPropagation();
    });

    document.documentElement.appendChild(host);

    hud = {
      host: host,
      root: root,
      meta: meta,
      sentence: sentence,
      position: position,
      track: track,
      fill: fill,
      message: message,
      pauseBtn: pauseBtn,
      stopBtn: stopBtn,
      lastState: null
    };
    return hud;
  }

  function hideHud() {
    if (hudHideTimer) {
      clearTimeout(hudHideTimer);
      hudHideTimer = null;
    }
    if (!hud) return;
    hud.host.style.setProperty('display', 'none', 'important');
    hud.lastState = null;
  }

  function renderHud(state) {
    if (!isTopFrame || !state || typeof state !== 'object') return;
    var h = ensureHud();
    if (!h) return;

    var status = T.STATUSES.indexOf(state.status) >= 0 ? state.status : 'error';
    if (status === 'done' && state.stopped === true) {
      hideHud();
      return;
    }

    if (hudHideTimer) {
      clearTimeout(hudHideTimer);
      hudHideTimer = null;
    }

    h.lastState = state;
    h.host.style.setProperty('display', 'block', 'important');
    h.host.dataset.status = status;
    h.host.dataset.index = String(
      T.clampInteger(state.index, 0, 1000000) || 0
    );
    h.host.dataset.total = String(
      T.clampInteger(state.total, 0, 1000000) || 0
    );
    if (state.provider) {
      h.host.dataset.provider = T.safeText(state.provider, 120);
    } else {
      delete h.host.dataset.provider;
    }
    if (state.voice) {
      h.host.dataset.voice = T.safeText(state.voice, 160);
    } else {
      delete h.host.dataset.voice;
    }

    var metaParts = [];
    if (state.provider) metaParts.push(T.safeText(state.provider, 120));
    if (state.voice) metaParts.push(T.safeText(state.voice, 160));
    var rateText = T.formatRate(state.rate);
    if (rateText) metaParts.push(rateText);
    var metaText = metaParts.length
      ? metaParts.join(' \u00b7 ')
      : 'Local bridge';
    if (h.meta.textContent !== metaText) h.meta.textContent = metaText;

    var text = T.trimMessage(state.text, 1200);
    if (!text) {
      if (status === 'preparing') text = 'Preparing audio\u2026';
      else if (status === 'paused') text = 'Paused';
      else if (status === 'done') text = 'Finished';
      else if (status === 'error') text = 'Could not read the selection.';
    }
    if (h.sentence.textContent !== text) h.sentence.textContent = text;

    var index = T.clampInteger(state.index, 0, 1000000) || 0;
    var total = T.clampInteger(state.total, 0, 1000000) || 0;
    var positionText = '';
    if (total > 0 && index > 0) {
      positionText =
        'Sentence ' + Math.min(index, total) + ' of ' + total;
    } else if (status === 'preparing') {
      positionText = 'Preparing\u2026';
    } else if (status === 'done') {
      positionText = 'Finished';
    } else if (status === 'error') {
      positionText = 'Stopped';
    }
    if (h.position.textContent !== positionText) {
      h.position.textContent = positionText;
    }

    var fraction = 0;
    if (status === 'done') {
      fraction = 1;
    } else if (total > 0 && index > 0) {
      var within = T.clampNumber(state.progress, 0, 1);
      fraction =
        ((index - 1) + (within === null ? 0 : within)) / total;
      fraction = Math.max(0, Math.min(1, fraction));
    }
    h.fill.style.width = (fraction * 100).toFixed(1) + '%';
    h.track.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));

    if (state.message) {
      var messageText = T.trimMessage(state.message, 1000);
      h.message.hidden = false;
      h.message.className =
        'message ' + (status === 'error' ? 'error' : 'notice');
      if (h.message.textContent !== messageText) {
        h.message.textContent = messageText;
      }
    } else {
      h.message.hidden = true;
      if (h.message.textContent) h.message.textContent = '';
    }

    var canPause =
      status === 'speaking' || status === 'paused' || status === 'preparing';
    h.pauseBtn.hidden = !canPause;
    h.pauseBtn.disabled = !canPause;
    h.pauseBtn.textContent = status === 'paused' ? 'Resume' : 'Pause';

    h.stopBtn.hidden = status === 'done' || status === 'error';

    if (status === 'done') {
      hudHideTimer = setTimeout(function () {
        hideHud();
      }, 4000);
    }
  }

  function handleHudControl(action) {
    if (action === 'pause') {
      pauseReading();
    } else if (action === 'resume') {
      resumeReading();
    } else if (action === 'toggle-pause') {
      if (session && session.paused) resumeReading();
      else pauseReading();
    } else if (action === 'stop' || action === 'close') {
      stopReadingInternal();
    }
  }

  /* ---------------------------------------------------------------- */
  /* Message handling                                                  */
  /* ---------------------------------------------------------------- */

  chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (
      !message ||
      typeof message !== 'object' ||
      typeof message.type !== 'string'
    ) {
      return;
    }

    switch (message.type) {
      case 'START_READ':
        startReading(message).then(
          function (result) {
            sendResponse(result);
          },
          function () {
            sendState({
              status: 'error',
              index: 0,
              total: 0,
              text: '',
              provider: null,
              voice: null,
              rate: null,
              message: 'The reader could not start on this page.'
            });
            sendResponse({
              ok: false,
              error: 'start_failed',
              message: 'The reader could not start on this page.'
            });
          }
        );
        return true;
      case 'STOP_READ':
        stopReadingInternal();
        sendResponse({ ok: true });
        return;
      case 'HUD_CONTROL':
        handleHudControl(message.action);
        sendResponse({ ok: true });
        return;
      case 'HUD_STATE':
        if (isTopFrame) renderHud(message.state);
        return;
      default:
        return;
    }
  });

  /* If the document goes away, tell the worker so it can clear the badge. */
  window.addEventListener(
    'pagehide',
    function () {
      if (!session) return;
      generation += 1;
      notifyBackground({ type: 'READ_GONE' });
      resetSession();
    },
    true
  );
})();
