/* Omarchy TTS Reader - options controller. Classic script. */
'use strict';

(function () {
  var T = self.OmarchyTTS;

  var form = document.getElementById('pairForm');
  var urlInput = document.getElementById('bridgeUrl');
  var tokenInput = document.getElementById('bridgeToken');
  var toggleToken = document.getElementById('toggleToken');
  var saveBtn = document.getElementById('saveBtn');
  var testBtn = document.getElementById('testBtn');
  var statusEl = document.getElementById('status');
  var infoBox = document.getElementById('bridgeInfo');
  var infoProvider = document.getElementById('infoProvider');
  var infoVoice = document.getElementById('infoVoice');
  var infoRate = document.getElementById('infoRate');

  var busy = false;

  function sendBackground(message, timeoutMs) {
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

  function setStatus(text, kind) {
    if (!text) {
      statusEl.hidden = true;
      statusEl.textContent = '';
      statusEl.className = 'status';
      return;
    }
    statusEl.hidden = false;
    statusEl.textContent = T.trimMessage(text, 700);
    statusEl.className = 'status ' + (kind || '');
  }

  function renderInfo(info) {
    if (!info) {
      infoBox.hidden = true;
      return;
    }
    infoBox.hidden = false;
    infoProvider.textContent = info.provider || '\u2014';
    infoVoice.textContent = info.voice || '\u2014';
    infoRate.textContent = T.formatRate(info.rate) || '\u2014';
  }

  function setBusy(value) {
    busy = value;
    saveBtn.disabled = value;
    testBtn.disabled = value;
    toggleToken.disabled = value;
  }

  function readForm() {
    var parsed = T.parseBridgeUrl(urlInput.value);
    if (!parsed) {
      setStatus(
        'Enter a bridge URL like http://127.0.0.1:8787. Only 127.0.0.1 is accepted.',
        'error'
      );
      urlInput.focus();
      return null;
    }
    return {
      bridgeUrl: T.bridgeBase(parsed),
      bridgeToken: tokenInput.value.trim()
    };
  }

  function loadSettings() {
    return new Promise(function (resolve) {
      chrome.storage.local.get(
        {
          bridgeUrl: T.DEFAULTS.bridgeUrl,
          bridgeToken: T.DEFAULTS.bridgeToken
        },
        function (items) {
          void chrome.runtime.lastError;
          resolve(items);
        }
      );
    });
  }

  function saveSettings(values) {
    return new Promise(function (resolve, reject) {
      chrome.storage.local.set(values, function () {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve();
        }
      });
    });
  }

  async function testConnection() {
    if (busy) return;
    var values = readForm();
    if (!values) return;

    setBusy(true);
    setStatus('Testing the bridge\u2026', '');
    renderInfo(null);
    try {
      var response = await sendBackground(
        {
          type: 'TEST_BRIDGE',
          bridgeUrl: values.bridgeUrl,
          bridgeToken: values.bridgeToken
        },
        12000
      );
      if (!response) {
        setStatus(
          'The extension did not answer. Reload this page and try again.',
          'error'
        );
        return;
      }
      if (response.ok !== true) {
        setStatus(
          response.message || 'Could not connect to the bridge.',
          'error'
        );
        return;
      }
      renderInfo(response.info);
      var parts = [];
      if (response.info && response.info.provider) {
        parts.push(response.info.provider);
      }
      if (response.info && response.info.voice) {
        parts.push(response.info.voice);
      }
      var suffix = parts.length ? ' \u2014 ' + parts.join(' / ') : '';
      setStatus('Connected to the local bridge' + suffix + '.', 'ok');
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (busy) return;
    var values = readForm();
    if (!values) return;

    setBusy(true);
    try {
      await saveSettings(values);
      urlInput.value = values.bridgeUrl;
      if (!values.bridgeToken) {
        setStatus(
          'Saved. Add a token from `speak-bridge --print-token` to connect.',
          ''
        );
      } else {
        setStatus('Saved. Testing the connection\u2026', '');
        setBusy(false);
        await testConnection();
      }
    } catch (err) {
      setStatus('Could not save settings: ' + err.message, 'error');
    } finally {
      setBusy(false);
    }
  });

  testBtn.addEventListener('click', function () {
    testConnection();
  });

  toggleToken.addEventListener('click', function () {
    var showing = tokenInput.type === 'text';
    tokenInput.type = showing ? 'password' : 'text';
    toggleToken.textContent = showing ? 'Show' : 'Hide';
    toggleToken.setAttribute('aria-pressed', showing ? 'false' : 'true');
    tokenInput.focus();
  });

  loadSettings().then(function (items) {
    urlInput.value = items.bridgeUrl || T.DEFAULTS.bridgeUrl;
    tokenInput.value = items.bridgeToken || '';
    if (tokenInput.value) {
      testConnection();
    } else {
      setStatus(
        'No token saved yet. Run `speak-bridge --print-token` and paste it above.',
        ''
      );
    }
  });
})();
