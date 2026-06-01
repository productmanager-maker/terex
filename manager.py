#!/usr/bin/env python3
"""
ttyd Multi-Tab Terminal Manager — port 7680

Each tab = independent tmux session with a fresh bash shell at
/workspace/projects. Users type `claude` themselves to start Claude Code.
Tabs do NOT share window state (no grouped sessions).
"""
import http.server, json, subprocess, threading, time
from urllib.parse import urlparse

HOST = '0.0.0.0'
PORT = 7680
TTYD_BIN = '/usr/bin/ttyd'
TTYD_INDEX = '/opt/ttyd/index.html'
MAX_SLOTS = 8
BASE_PORT = 7681
SESSION_PREFIX = 'slot-'
WORK_DIR = '/workspace/projects'
THEME = '{"background":"#F8F8F8","foreground":"#2A2B33","cursor":"#2F5AF3","cursorAccent":"#F8F8F8","selectionBackground":"rgba(75,160,255,0.28)","selectionForeground":"#000000","black":"#000000","red":"#DE3D35","green":"#3E953A","yellow":"#C18A00","blue":"#2F5AF3","magenta":"#A00095","cyan":"#0083BF","white":"#BBBBBB","brightBlack":"#5C6370","brightRed":"#DE3D35","brightGreen":"#3E953A","brightYellow":"#C18A00","brightBlue":"#2F5AF3","brightMagenta":"#A00095","brightCyan":"#0083BF","brightWhite":"#FFFFFF"}'

slots = {}   # slot_id -> {'port', 'proc', 'session', 'name'}
lock = threading.Lock()


def tmux(*args):
    return subprocess.run(['docker', 'exec', 'code-server', 'tmux'] + list(args),
                          capture_output=True, text=True)


def session_exists(session):
    return tmux('has-session', '-t', session).returncode == 0


def list_slot_sessions():
    r = tmux('list-sessions', '-F', '#{session_name}')
    out = []
    for line in r.stdout.strip().split('\n'):
        if line.startswith(SESSION_PREFIX):
            try:
                out.append(int(line[len(SESSION_PREFIX):]))
            except ValueError:
                pass
    return sorted(out)


def ensure_session(slot, name='shell'):
    """Create independent tmux session for slot if it doesn't exist."""
    session = f'{SESSION_PREFIX}{slot}'
    if not session_exists(session):
        # Independent session, fresh bash at WORK_DIR. No -t grouping.
        tmux('new-session', '-d', '-s', session, '-n', name, '-c', WORK_DIR)
    return session


def spawn_ttyd(slot, name='shell'):
    """Ensure tmux session + ttyd are running for this slot."""
    with lock:
        existing = slots.get(slot)
        if existing and existing['proc'].poll() is None:
            return existing
        session = ensure_session(slot, name)
        port = BASE_PORT + slot
        base = f'/t/{slot}'
        cmd = [
            TTYD_BIN, '--port', str(port),
            '--writable', '--index', TTYD_INDEX,
            '--base-path', base,
            '--client-option', 'fontSize=15',
            '--client-option', f'theme={THEME}',
            'docker', 'exec', '-it',
            '-e', 'COLORTERM=truecolor', '-e', 'TERM=xterm-256color',
            'code-server', 'tmux', 'attach-session', '-t', session,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        slots[slot] = {'port': port, 'proc': proc, 'session': session, 'name': name}
        time.sleep(0.4)
        return slots[slot]


def kill_slot(slot):
    with lock:
        info = slots.pop(slot, None)
        session = info['session'] if info else f'{SESSION_PREFIX}{slot}'
        if info:
            try:
                info['proc'].terminate()
            except Exception:
                pass
        tmux('kill-session', '-t', session)


def find_free_slot():
    used = set(slots.keys()) | set(list_slot_sessions())
    for i in range(MAX_SLOTS):
        if i not in used:
            return i
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def jsend(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/windows':
            result = []
            for idx in list_slot_sessions():
                if idx < MAX_SLOTS:
                    info = spawn_ttyd(idx)
                    result.append({'index': idx, 'name': info.get('name', 'shell')})
            if not result:
                info = spawn_ttyd(0)
                result.append({'index': 0, 'name': info.get('name', 'shell')})
            self.jsend(result)
        elif path in ('/', '/index.html'):
            with open('/opt/ttyd-manager/ui.html', 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/windows':
            n = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(n)
            data = json.loads(body) if body else {}
            name = data.get('name', 'shell')
            slot = find_free_slot()
            if slot is None:
                self.jsend({'error': 'max tabs'}, 400)
                return
            info = spawn_ttyd(slot, name)
            self.jsend({'index': slot, 'name': info.get('name', name)})
        else:
            self.send_response(404)
            self.end_headers()


    def do_PATCH(self):
        parts = urlparse(self.path).path.strip('/').split('/')
        if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'windows':
            slot = int(parts[2])
            n = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(n)
            data = json.loads(body) if body else {}
            name = (data.get('name') or 'shell').strip() or 'shell'
            with lock:
                if slot in slots:
                    slots[slot]['name'] = name
                    session = slots[slot]['session']
                    tmux('rename-window', '-t', session + ':0', name)
                    self.jsend({'index': slot, 'name': name})
                else:
                    self.jsend({'error': 'slot not found'}, 404)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parts = urlparse(self.path).path.strip('/').split('/')
        if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'windows':
            slot = int(parts[2])
            kill_slot(slot)
            self.jsend({'ok': True})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    for idx in list_slot_sessions():
        if idx < MAX_SLOTS:
            spawn_ttyd(idx)
    if not slots:
        spawn_ttyd(0)
    print(f'ttyd-manager on :{PORT}')
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
