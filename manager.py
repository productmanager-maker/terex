#!/usr/bin/env python3
"""
ttyd Multi-Tab Terminal Manager — port 7680

Each tab = independent tmux session with a fresh bash shell at
/workspace/projects. Users type `claude` themselves to start Claude Code.
Tabs do NOT share window state (no grouped sessions).
"""
import http.server, json, os, secrets as _secrets, subprocess, threading, time
from urllib.parse import urlparse

HOST = '0.0.0.0'
MANAGER_TOKEN = os.environ.get('MANAGER_TOKEN', '4f7eeb804fc1bd6788aeed535c939e919c513ca90ca3299ed6239d415501c913')
PIN = os.environ.get('TEREX_PIN', '208659')
PORT = 7680
TTYD_BIN = '/usr/bin/ttyd'
TTYD_INDEX = '/opt/ttyd/index.html'
MAX_SLOTS = 8
BASE_PORT = 7681
SESSION_PREFIX = 'slot-'
WORK_DIR = '/workspace/projects'
THEME = '{"background":"#F8F8F8","foreground":"#2A2B33","cursor":"#2F5AF3","cursorAccent":"#F8F8F8","selectionBackground":"rgba(75,160,255,0.28)","selectionForeground":"#000000","black":"#000000","red":"#DE3D35","green":"#3E953A","yellow":"#C18A00","blue":"#2F5AF3","magenta":"#A00095","cyan":"#0083BF","white":"#BBBBBB","brightBlack":"#5C6370","brightRed":"#DE3D35","brightGreen":"#3E953A","brightYellow":"#C18A00","brightBlue":"#2F5AF3","brightMagenta":"#A00095","brightCyan":"#0083BF","brightWhite":"#FFFFFF"}'

_pin_sessions = set()

PIN_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Terminal — PIN</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#1e1e2e;display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',system-ui,sans-serif}
.card{background:#fff;border-radius:16px;padding:32px 28px;width:90%;max-width:300px;box-shadow:0 12px 40px rgba(0,0,0,.4);text-align:center}
h2{font-size:17px;font-weight:700;color:#111;margin-bottom:6px}
p{font-size:13px;color:#888;margin-bottom:22px}
.dots{display:flex;gap:14px;justify-content:center;margin-bottom:24px}
.dot{width:13px;height:13px;border-radius:50%;background:#e0e0e0;transition:background .15s}
.dot.on{background:#2F5AF3}
.dot.err{background:#FF3B30}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.k{padding:15px;border-radius:10px;border:none;background:#f5f5f5;font-size:19px;font-weight:500;cursor:pointer;color:#111;transition:background .1s;-webkit-tap-highlight-color:transparent}
.k:active,.k:focus{background:#e0e0e0;outline:none}
.k.del{font-size:14px;color:#555}
.k.blank{background:transparent;cursor:default;pointer-events:none}
.msg{font-size:12px;color:#FF3B30;margin-top:14px;min-height:16px}
</style>
</head>
<body>
<div class="card">
  <h2>Masukkan PIN</h2>
  <p>Verifikasi untuk melanjutkan</p>
  <div class="dots" id="dots"></div>
  <div class="pad" id="pad"></div>
  <div class="msg" id="msg"></div>
</div>
<script>
const LEN=6;let pin='';
const dots=document.getElementById('dots');
const msg=document.getElementById('msg');
for(let i=0;i<LEN;i++){const d=document.createElement('div');d.className='dot';d.id='d'+i;dots.appendChild(d);}
[1,2,3,4,5,6,7,8,9,'blank',0,'del'].forEach(k=>{
  const b=document.createElement('button');
  if(k==='blank'){b.className='k blank';b.disabled=true;b.textContent='';}
  else if(k==='del'){b.className='k del';b.textContent='\\u232b';}
  else{b.className='k';b.textContent=k;}
  b.addEventListener('click',()=>{
    if(k==='blank')return;
    if(k==='del'){pin=pin.slice(0,-1);msg.textContent='';render(false);return;}
    if(pin.length>=LEN)return;
    pin+=k;render(false);
    if(pin.length===LEN)submit();
  });
  document.getElementById('pad').appendChild(b);
});
function render(err){
  for(let i=0;i<LEN;i++)
    document.getElementById('d'+i).className='dot'+(i<pin.length?(err?' err':' on'):'');
}
async function submit(){
  const r=await fetch('/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:''+pin})});
  if(r.ok){location.href='/';}
  else{msg.textContent='PIN salah. Coba lagi.';render(true);setTimeout(()=>{pin='';render(false);msg.textContent='';},1300);}
}
document.addEventListener('keydown',e=>{
  if(e.key>='0'&&e.key<='9'&&pin.length<LEN){pin+=e.key;render(false);if(pin.length===LEN)submit();}
  else if(e.key==='Backspace'){pin=pin.slice(0,-1);msg.textContent='';render(false);}
});
</script>
</body>
</html>'''

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

    def check_auth(self):
        """Verify request came through Caddy (internal token)."""
        if self.headers.get('X-Manager-Token') == MANAGER_TOKEN:
            return True
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')
        return False

    def check_pin_session(self):
        """Verify user has passed the PIN gate."""
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k.strip() == 'terex_sid' and v.strip() in _pin_sessions:
                return True
        return False

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
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PATCH,DELETE,OPTIONS')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            if not self.check_pin_session():
                body = PIN_PAGE.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            with open('/opt/ttyd-manager/ui.html', 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/windows':
            if not self.check_auth():
                return
            result = []
            for idx in list_slot_sessions():
                if idx < MAX_SLOTS:
                    info = spawn_ttyd(idx)
                    result.append({'index': idx, 'name': info.get('name', 'shell')})
            if not result:
                info = spawn_ttyd(0)
                result.append({'index': 0, 'name': info.get('name', 'shell')})
            self.jsend(result)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/auth':
            n = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(n)
            data = json.loads(body) if body else {}
            if str(data.get('pin', '')) == PIN:
                token = _secrets.token_hex(24)
                _pin_sessions.add(token)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Set-Cookie', f'terex_sid={token}; Path=/; HttpOnly; SameSite=Strict')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self.jsend({'error': 'wrong pin'}, 403)
            return
        if not self.check_auth():
            return
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
        if not self.check_auth():
            return
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
        if not self.check_auth():
            return
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
