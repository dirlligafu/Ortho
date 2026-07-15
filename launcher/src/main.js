const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

function el(id) { return document.getElementById(id); }

function setStep(id, state, text) {
  const step = el('step-' + id);
  if (!step) return;
  step.className = 'step ' + state;
  const icon = state === 'done' ? '✓' : state === 'error' ? '✗' : '⋯';
  step.textContent = icon + ' ' + text;
  step.classList.remove('hidden');
}

function showError(msg) {
  const box = el('error-box');
  box.textContent = typeof msg === 'string' ? msg : JSON.stringify(msg);
  box.classList.remove('hidden');
}

function appendLog(line) {
  const log = el('pip-log');
  log.classList.remove('hidden');
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
}

function waitForFlask(url, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;
    const check = () => {
      if (Date.now() > deadline) {
        reject(new Error('App failed to start after 30 seconds. Port 5000 may already be in use, or check the log above for details.'));
        return;
      }
      fetch(url, { mode: 'no-cors' })
        .then(resolve)
        .catch(() => setTimeout(check, 500));
    };
    setTimeout(check, 500);
  });
}

async function launch() {
  const flaskUrl = await invoke('flask_url');

  // 1. Find Python
  setStep('python', 'active', 'Checking Python...');
  let python;
  try {
    python = await invoke('find_python');
    setStep('python', 'done', `Python found (${python})`);
  } catch (e) {
    setStep('python', 'error', 'Python not found');
    showError(String(e));
    return;
  }

  // 2. Dependencies
  el('step-deps').classList.remove('hidden');
  setStep('deps', 'active', 'Checking dependencies...');
  const needInstall = await invoke('deps_need_install');
  if (needInstall) {
    setStep('deps', 'active', 'Installing dependencies — first run, may take a few minutes...');
    const unlisten = await listen('pip-output', e => appendLog(e.payload));
    try {
      await invoke('install_deps', { python });
      unlisten();
      setStep('deps', 'done', 'Dependencies ready');
    } catch (e) {
      unlisten();
      setStep('deps', 'error', 'Dependency installation failed');
      showError(String(e));
      return;
    }
  } else {
    setStep('deps', 'done', 'Dependencies ready');
  }

  // 3. Launch Flask
  el('step-flask').classList.remove('hidden');
  setStep('flask', 'active', 'Starting app...');
  const unlistenFlaskError = await listen('flask-error', e => appendLog(e.payload));
  try {
    await invoke('launch_flask', { python });
  } catch (e) {
    unlistenFlaskError();
    setStep('flask', 'error', 'Failed to start the app');
    showError(String(e));
    return;
  }

  try {
    await waitForFlask(flaskUrl);
  } catch (e) {
    unlistenFlaskError();
    setStep('flask', 'error', 'App failed to start');
    showError(String(e));
    return;
  }
  unlistenFlaskError();
  setStep('flask', 'done', 'App ready');

  // Navigate the Tauri window directly to Flask (no iframe — needed for
  // drag-and-drop and file downloads to work properly in the WebView)
  await invoke('navigate_to_flask');
}

launch();
