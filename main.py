import os
import sys
import re
import time
import threading
import subprocess
from collections import deque

# ---- Self-healing: install Flask & requests if missing ----
try:
    from flask import Flask, request, Response, jsonify
    import requests
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "flask", "requests"], check=False)
    from flask import Flask, request, Response, jsonify
    import requests

app = Flask(__name__)

UPLOAD_FOLDER = 'uploaded_scripts'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---- Global state ----
running_process = None
current_filepath = None
process_logs = deque(maxlen=3000)
next_log_id = 0
log_lock = threading.Lock()
log_event = threading.Event()

current_session = 0
session_lock = threading.Lock()

# ---- Common package aliases (missing name -> real pip name) ----
PACKAGE_ALIASES = {
    'PIL': 'Pillow',
    'cv2': 'opencv-python',
    'sklearn': 'scikit-learn',
    'bs4': 'beautifulsoup4',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'telegram': 'python-telegram-bot',
    'telebot': 'pyTelegramBotAPI',
    'aiogram': 'aiogram',
    'pyrogram': 'pyrogram',
    'discord': 'discord.py',
    'nextcord': 'nextcord',
    'disnake': 'disnake',
    'hikari': 'hikari',
    'dateutil': 'python-dateutil',
    'jwt': 'PyJWT',
    'Crypto': 'pycryptodome',
    'OpenSSL': 'pyOpenSSL',
    'tgcrypto': 'TgCrypto',
    'pytgcalls': 'py-tgcalls',
    'ffmpeg': 'ffmpeg-python',
    'psycopg2': 'psycopg2-binary',
    'sqlalchemy': 'SQLAlchemy',
    'uvloop': 'uvloop',
    'pymongo': 'pymongo',
    'motor': 'motor',
    'redis': 'redis',
    'httpx': 'httpx',
    'aiohttp': 'aiohttp',
    'fastapi': 'fastapi',
    'uvicorn': 'uvicorn',
    'numpy': 'numpy',
    'pandas': 'pandas',
}


def add_log(msg):
    global next_log_id
    with log_lock:
        process_logs.append((next_log_id, msg))
        next_log_id += 1
    log_event.set()


def _kill_running():
    global running_process
    proc = running_process
    running_process = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
        except Exception:
            pass


def _pip_install(pkg):
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "--no-input", pkg],
            capture_output=True, text=True, timeout=900
        )
        return r.returncode == 0, ((r.stderr or '') + (r.stdout or ''))[-600:]
    except Exception as e:
        return False, str(e)


def _run_script(filepath, my_session):
    global running_process
    max_attempts = 25

    for attempt in range(1, max_attempts + 1):
        with session_lock:
            if my_session != current_session:
                return

        add_log(f"[SYSTEM] Starting script (attempt {attempt})...")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", filepath],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                env=env,
            )
        except Exception as e:
            add_log(f"[SYSTEM] Launch error: {e}")
            return

        running_process = proc
        recent = []

        try:
            for raw in iter(proc.stdout.readline, ''):
                if raw == '' or raw is None:
                    break
                line = raw.rstrip('\n')
                if line == '':
                    continue
                add_log(line)
                recent.append(line)
                if len(recent) > 80:
                    recent.pop(0)
        except Exception as e:
            add_log(f"[SYSTEM] Read error: {e}")

        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc = -1

        if running_process is proc:
            running_process = None

        with session_lock:
            if my_session != current_session:
                return

        pattern = re.compile(
            r"(?:ModuleNotFoundError|ImportError):\s*No module named ['\"]?([A-Za-z_][\w\.]*)"
        )
        missing = None
        for line in recent:
            m = pattern.search(line)
            if m:
                missing = m.group(1).split('.')[0]
                break

        if missing:
            pkg = PACKAGE_ALIASES.get(missing, missing)
            add_log(f"[AUTO-INSTALL] Missing package: '{missing}' -> installing '{pkg}'...")
            ok, info = _pip_install(pkg)

            if ok:
                add_log(f"[AUTO-INSTALL] '{pkg}' installed successfully. Restarting...")
                continue

            if pkg != missing:
                add_log(f"[AUTO-INSTALL] Retrying with raw name '{missing}'...")
                ok2, info2 = _pip_install(missing)
                if ok2:
                    add_log(f"[AUTO-INSTALL] '{missing}' installed. Restarting...")
                    continue

            add_log(f"[AUTO-INSTALL] Could not install '{pkg}'. Details: {info.strip()}")
            return

        if rc == 0:
            add_log("[SYSTEM] Process finished.")
        elif rc != -1:
            add_log(f"[SYSTEM] Process exited with code {rc}.")
        return

    add_log("[SYSTEM] Gave up after too many auto-install attempts.")


def keep_alive():
    while True:
        time.sleep(240)
        try:
            url = (
                os.environ.get("RENDER_EXTERNAL_URL")
                or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
            )
            if url and not url.startswith("http"):
                url = "https://" + url
            if url:
                requests.get(url, timeout=15)
                print(f"[KEEP-ALIVE] Ping sent -> {url}")
        except Exception as e:
            print(f"[KEEP-ALIVE] {e}")


# ============================================================
#  UI
# ============================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Python Bot Hosting Panel</title>
<style>
:root {
  --bg: #0f172a; --card: #1e293b; --accent: #3b82f6;
  --success: #10b981; --danger: #ef4444; --text: #f8fafc;
  --muted: #94a3b8; --term: #000000; --border: #334155;
  --warn: #f59e0b;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); margin: 0; padding: 16px;
  min-height: 100vh;
}
.container {
  max-width: 880px; margin: 0 auto; background: var(--card);
  border-radius: 14px; padding: 22px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.5);
}
h1 { text-align: center; font-size: 1.5rem; margin: 0 0 18px; color: #60a5fa; }
.upload-box {
  border: 2px dashed var(--border); border-radius: 10px;
  padding: 22px 16px; text-align: center; background: var(--bg);
  margin-bottom: 16px; transition: 0.2s;
}
.upload-box:hover { border-color: var(--accent); }
input[type=file] { display: none; }
.file-label {
  display: inline-block; padding: 11px 22px; background: var(--accent);
  color: white; border-radius: 8px; font-weight: 600; cursor: pointer;
  transition: 0.2s;
}
.file-label:hover { background: #2563eb; }
.file-name {
  margin-top: 12px; color: var(--muted); font-style: italic;
  font-size: 0.92rem; word-break: break-all;
}
.buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  flex: 1; min-width: 100px; padding: 12px 16px; border: none;
  border-radius: 8px; font-size: 15px; font-weight: 600;
  color: white; cursor: pointer; transition: 0.2s;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-run { background: var(--success); }
.btn-run:hover:not(:disabled) { background: #059669; }
.btn-restart { background: var(--warn); }
.btn-restart:hover:not(:disabled) { background: #d97706; }
.btn-stop { background: var(--danger); }
.btn-stop:hover:not(:disabled) { background: #dc2626; }
.btn-clear { background: #475569; }
.btn-clear:hover { background: #64748b; }

.status-row {
  display: flex; justify-content: space-between; align-items: center;
  margin: 18px 0 8px; font-size: 0.9rem; color: var(--muted);
  font-weight: 600; gap: 10px; flex-wrap: wrap;
}
.status-left { display: flex; align-items: center; gap: 8px; }
.dot {
  width: 10px; height: 10px; border-radius: 50%; background: #64748b;
  box-shadow: 0 0 0 0 rgba(100,116,139,0.7);
}
.dot.running {
  background: var(--success);
  animation: pulse 1.6s infinite;
}
.dot.error { background: var(--danger); }
.dot.installing { background: var(--warn); animation: pulse 1.6s infinite; }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.6); }
  70% { box-shadow: 0 0 0 10px rgba(16,185,129,0); }
  100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
}
.file-info {
  font-size: 0.82rem; color: var(--muted); font-style: italic;
  max-width: 50%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
#terminal {
  background: var(--term); color: #4ade80;
  font-family: 'Courier New', Consolas, monospace;
  font-size: 13px; padding: 14px; border-radius: 8px; height: 360px;
  overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  border: 1px solid var(--border); line-height: 1.5;
}
#terminal div { white-space: pre-wrap; word-break: break-word; }
@media (max-width: 480px) {
  body { padding: 8px; }
  .container { padding: 16px; }
  h1 { font-size: 1.25rem; }
  #terminal { height: 300px; font-size: 12px; }
  .btn { font-size: 14px; padding: 11px; min-width: 80px; }
  .file-info { max-width: 100%; }
}
</style>
</head>
<body>
<div class="container">
  <h1>🐍 Python Bot Hosting Panel</h1>

  <div class="upload-box">
    <label for="pyfile" class="file-label">📄 Select Python File (.py)</label>
    <input type="file" id="pyfile" accept=".py" onchange="onFileSelect()">
    <div class="file-name" id="fileName">No file selected</div>
  </div>

  <div class="buttons">
    <button class="btn btn-run" id="runBtn" onclick="runFile()">▶ Run</button>
    <button class="btn btn-restart" onclick="restartFile()">🔄 Restart</button>
    <button class="btn btn-stop" onclick="stopFile()">⏹ Stop</button>
    <button class="btn btn-clear" onclick="clearTerminal()">🗑 Clear</button>
  </div>

  <div class="status-row">
    <div class="status-left">
      <span class="dot" id="statusDot"></span>
      <span id="statusText">Ready</span>
    </div>
    <div class="file-info" id="fileInfo">No file running</div>
  </div>
  <div id="terminal"><div>System ready. Please select a .py file to start.</div></div>
</div>

<script>
let selectedFile = null;
const term = document.getElementById('terminal');
const MAX_LINES = 1500;

function setStatus(text, cls) {
  document.getElementById('statusText').textContent = text;
  const dot = document.getElementById('statusDot');
  dot.className = 'dot' + (cls ? ' ' + cls : '');
}

function setFileInfo(name) {
  document.getElementById('fileInfo').textContent = name || 'No file running';
}

function onFileSelect() {
  const input = document.getElementById('pyfile');
  const nameEl = document.getElementById('fileName');
  if (input.files.length > 0) {
    selectedFile = input.files[0];
    nameEl.textContent = 'Selected: ' + selectedFile.name;
  } else {
    selectedFile = null;
    nameEl.textContent = 'No file selected';
  }
}

function runFile() {
  if (!selectedFile) { alert('Please select a .py file first.'); return; }
  const form = new FormData();
  form.append('file', selectedFile);

  document.getElementById('runBtn').disabled = true;
  setStatus('Uploading...', 'installing');

  fetch('/upload_and_run', { method: 'POST', body: form })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        clearTerminal();
        setStatus('Running', 'running');
        setFileInfo(data.file || '');
      } else {
        setStatus('Error', 'error');
        alert(data.message || 'Upload failed');
      }
    })
    .catch(err => { setStatus('Error', 'error'); alert('Network error: ' + err); })
    .finally(() => { document.getElementById('runBtn').disabled = false; });
}

function restartFile() {
  setStatus('Restarting...', 'installing');
  fetch('/restart', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'success') {
        clearTerminal();
        setStatus('Running', 'running');
        setFileInfo(data.file || '');
      } else {
        setStatus('Error', 'error');
        alert(data.message || 'Restart failed');
      }
    })
    .catch(err => { setStatus('Error', 'error'); alert('Network error: ' + err); });
}

function stopFile() {
  setStatus('Stopping...', 'installing');
  fetch('/stop', { method: 'POST' })
    .then(r => r.json())
    .then(() => { setStatus('Stopped', ''); setFileInfo(''); })
    .catch(err => console.error(err));
}

function clearTerminal() {
  term.innerHTML = '';
}

function appendLog(line) {
  const div = document.createElement('div');
  div.textContent = line;
  term.appendChild(div);
  while (term.childElementCount > MAX_LINES) {
    term.removeChild(term.firstChild);
  }
  term.scrollTop = term.scrollHeight;
}

const es = new EventSource('/stream_logs');

es.onmessage = function(e) {
  const line = e.data;
  appendLog(line);

  if (line.indexOf('[AUTO-INSTALL]') !== -1) setStatus('Installing...', 'installing');
  else if (line.indexOf('[SYSTEM] Starting script') !== -1) setStatus('Running', 'running');
  else if (line.indexOf('[SYSTEM] Process finished') !== -1 ||
           line.indexOf('exited with code') !== -1) setStatus('Stopped', '');
};
es.onerror = function() { /* browser auto-reconnects */ };

// Refresh status every 3s
function refreshStatus() {
  fetch('/status').then(r => r.json()).then(s => {
    if (s.file) setFileInfo(s.file);
    if (s.running) {
      setStatus('Running' + (s.pid ? ' (PID ' + s.pid + ')' : ''), 'running');
    }
  }).catch(() => {});
}
setInterval(refreshStatus, 3000);
</script>
</body>
</html>"""


# ============================================================
#  Routes
# ============================================================

@app.route('/')
def home():
    return Response(HTML_TEMPLATE, mimetype='text/html')


@app.route('/upload_and_run', methods=['POST'])
def upload_and_run():
    global current_session, current_filepath

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file uploaded.'}), 400

    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.py'):
        return jsonify({'status': 'error', 'message': 'Only .py files are allowed.'}), 400

    safe_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', f.filename)
    if not safe_name.endswith('.py'):
        safe_name += '.py'
    filepath = os.path.join(UPLOAD_FOLDER, safe_name)
    f.save(filepath)

    _kill_running()

    with log_lock:
        process_logs.clear()
    log_event.set()

    with session_lock:
        current_session += 1
        my_session = current_session

    current_filepath = filepath
    add_log(f"[SYSTEM] Uploaded file: {safe_name}")

    threading.Thread(target=_run_script, args=(filepath, my_session), daemon=True).start()

    return jsonify({'status': 'success', 'message': f'{safe_name} uploaded and running.', 'file': safe_name})


@app.route('/restart', methods=['POST'])
def restart():
    global current_session

    filepath = current_filepath
    if not filepath or not os.path.exists(filepath):
        return jsonify({'status': 'error', 'message': 'No previous file to restart. Upload first.'}), 400

    _kill_running()

    with log_lock:
        process_logs.clear()
    log_event.set()

    with session_lock:
        current_session += 1
        my_session = current_session

    safe_name = os.path.basename(filepath)
    add_log(f"[SYSTEM] Restarting: {safe_name}")

    threading.Thread(target=_run_script, args=(filepath, my_session), daemon=True).start()

    return jsonify({'status': 'success', 'message': f'Restarting {safe_name}', 'file': safe_name})


@app.route('/stop', methods=['POST'])
def stop():
    global current_session

    with session_lock:
        current_session += 1
    _kill_running()
    add_log("[SYSTEM] Stop requested by user.")
    return jsonify({'status': 'success'})


@app.route('/status')
def status():
    proc = running_process
    running = proc is not None and proc.poll() is None
    return jsonify({
        'running': running,
        'pid': proc.pid if running else None,
        'file': os.path.basename(current_filepath) if current_filepath else None,
    })


@app.route('/stream_logs')
def stream_logs():
    last_event_id = request.headers.get('Last-Event-ID')
    try:
        initial_last_id = int(last_event_id) if last_event_id else -1
    except (ValueError, TypeError):
        initial_last_id = -1

    def generate():
        last_id = initial_last_id
        try:
            while True:
                log_event.clear()
                with log_lock:
                    items = list(process_logs)
                for (i, m) in items:
                    if i <= last_id:
                        continue
                    # Single SSE event; multi-line data is joined with 'data:' prefix
                    data = m.replace('\n', '\ndata: ')
                    yield f"id: {i}\ndata: {data}\n\n"
                    last_id = i
                log_event.wait(timeout=0.4)
        except GeneratorExit:
            return

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ============================================================
#  Main
# ============================================================

if __name__ == '__main__':
    threading.Thread(target=keep_alive, daemon=True).start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)