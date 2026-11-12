/* Omarchy TTS Reader - popup controller. Classic script. */
'use strict';

(function () {
  var T = self.OmarchyTTS;

  var dot = document.getElementById('statusDot');
  var connection = document.getElementById('connection');
  var info = document.getElementById('info');
  var providerEl = document.getElementById('provider');
  var voiceEl = document.getElementById('voice');
  var rateEl = document.getElementById('rate');
  var activity = document.getElementById('activity');
  var readBtn = document.getElementById('readBtn');
  var stopBtn = document.getElementById('stopBtn');
  var messageEl = document.getElementById('message');
  var optionsBtn = document.getElementById('optionsBtn');

  var busy = false;
  var pollTimer = null;
  var statePending = false;

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

  function showMessage(text) {
    if (!text) {
      messageEl.hidden = true;
      messageEl.textContent = '';
      return;
    }
    messageEl.hidden = false;
    messageEl.textContent = T.trimMessage(text, 600);
  }

  function setBusy(value) {
    busy = value;
    readBtn.disabled = value;
    stopBtn.disabled = value || stopBtn.dataset.active !== '1';
  }

  function setConnected(ok, text) {
    dot.className = 'dot ' + (ok ? 'ok' : 'bad');
    connection.textContent = text;
  }

  function renderInfo(data) {
    if (!data) {
      info.hidden = true;
      return;
    }
    info.hidden = false;
    providerEl.textContent = data.provider || '\u2014';
    voiceEl.textContent = data.voice || '\u2014';
    rateEl.textContent = T.formatRate(data.rate) || '\u2014';
  }

  function setActive(active) {
    stopBtn.dataset.active = active ? '1' : '0';
    stopBtn.disabled = !active || busy;
    readBtn.textContent = active ? 'Restart reading' : 'Read selection';
  }

  function renderReadState(response) {
    if (!response || response.ok !== true) return;
    var state = response.state || null;

    if (response.active && state) {
      setActive(true);
      var index = T.clampInteger(state.index, 0, 1000000) || 0;
      var total = T.clampInteger(state.total, 0, 1000000) || 0;
      activity.hidden = false;
      if (state.status === 'preparing') {
        activity.textContent = 'Preparing audio\u2026';
      } else if (state.status === 'paused') {
        activity.textContent =
          total && index ? 'Paused at sentence ' + index + ' of ' + total : 'Paused';
      } else if (state.status === 'speaking') {
        activity.textContent =
          total && index ? 'Reading sentence ' + index + ' of ' + total : 'Reading';
      } else {
        activity.textContent = 'Reading\u2026';
      }
      return;
    }

    setActive(false);
    activity.hidden = true;
    activity.textContent = '';

    if (state && state.status === 'error' && state.message) {
      showMessage(state.message);
    }
  }

  async function refreshReadState() {
    if (statePending || document.hidden) return;
    statePending = true;
    try {
      var response = await sendBackground(
        { type: 'GET_READ_STATE' },
        4000
      );
      renderReadState(response);
    } finally {
      statePending = false;
    }
  }

  async function refreshBridge() {
    var response = await sendBackground({ type: 'TEST_BRIDGE' }, 12000);
    if (response && response.ok === true) {
      setConnected(true, 'Connected to the local bridge');
      renderInfo(response.info);
      return;
    }
    var text =
      response && response.message
        ? response.message
        : 'Cannot reach the bridge. Open Settings to check the URL and token.';
    setConnected(false, text);
    renderInfo(null);
  }

  readBtn.addEventListener('click', async function () {
    if (busy) return;
    setBusy(true);
    showMessage('');
    try {
      var response = await sendBackground({ type: 'START_READ' }, 10000);
      if (!response) {
        showMessage(
          'The extension did not answer. Reload the page and try again.'
        );
      } else if (response.ok !== true) {
        showMessage(
          response.message || 'Select some text on the page first.'
        );
      } else {
        activity.hidden = false;
        activity.textContent = 'Starting\u2026';
      }
    } finally {
      setBusy(false);
      refreshReadState();
    }
  });

  stopBtn.addEventListener('click', async function () {
    if (busy) return;
    setBusy(true);
    try {
      await sendBackground({ type: 'STOP_READ' }, 6000);
    } finally {
      setBusy(false);
      refreshReadState();
    }
  });

  optionsBtn.addEventListener('click', function () {
    try {
      chrome.runtime.sendMessage({ type: 'OPEN_OPTIONS' }, function () {
        void chrome.runtime.lastError;
      });
    } catch (err) {
      /* ignore */
    }
    window.close();
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refreshReadState();
  });

  readBtn.disabled = false;
  setActive(false);

  refreshBridge();
  refreshReadState();
  pollTimer = setInterval(refreshReadState, 800);

  window.addEventListener('unload', function () {
    if (pollTimer) clearInterval(pollTimer);
  });
})();
