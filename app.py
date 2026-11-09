#!/usr/bin/env python3
"""Minecraft server dashboard. Runs next to the server and serves a mobile-friendly web UI."""
import glob
import hmac
import os
import re
import secrets
import subprocess
import tarfile
import threading
import time
from functools import wraps
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, request, session

MC = Path(os.environ.get("MC_DIR", str(Path.home() / "mc")))
PASS_FILE = MC / "dash_pass.txt"
KEY_FILE = MC / ".dash_secret"
LOG = MC / "logs" / "latest.log"
PORT = int(os.environ.get("DASH_PORT", "8080"))

if not PASS_FILE.exists():
    raise SystemExit("dash_pass.txt missing - run install.sh first")
PASSWORD = PASS_FILE.read_text().strip()
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32))

app = Flask(__name__)
app.secret_key = KEY_FILE.read_text().strip()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=7 * 24 * 3600)

NAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,20}$")
JOIN_RE = re.compile(r"\]: (\S+) joined the game")
LEAVE_RE = re.compile(r"\]: (\S+) left the game")
busy = {"backup": False}


# ---------- helpers ----------
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def java_proc():
    for p in psutil.process_iter(["cmdline"]):
        try:
            if p.info["cmdline"] and any("paper.jar" in a for a in p.info["cmdline"]):
                return p
        except Exception:
            pass
    return None


def send(cmd):
    run(["screen", "-S", "mc", "-p", "0", "-X", "stuff", cmd + "\r"])


def start_server():
    if java_proc():
        return
    run(["screen", "-dmS", "mc", str(MC / "start.sh")])


def wait_stop(timeout=90):
    for _ in range(timeout):
        if not java_proc():
            return True
        time.sleep(1)
    return False


def tail_lines(path, n=150):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 120000))
            data = f.read().decode("utf-8", "ignore")
        return data.splitlines()[-n:]
    except Exception:
        return []


def online_players():
    players = []
    for line in tail_lines(LOG, 2000):
        m = JOIN_RE.search(line)
        if m and m.group(1) not in players:
            players.append(m.group(1))
            continue
        m = LEAVE_RE.search(line)
        if m and m.group(1) in players:
            players.remove(m.group(1))
        if "Stopping server" in line:
            players = []
    return players


def temperature():
    temps = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            v = float(Path(p).read_text().strip())
            temps.append(v / 1000 if v > 1000 else v)
        except Exception:
            pass
    return round(max(temps), 1) if temps else None


def battery():
    try:
        return int(Path("/sys/class/power_supply/battery/capacity").read_text().strip())
    except Exception:
        return None


def do_backup():
    if busy["backup"]:
        return
    busy["backup"] = True
    running = bool(java_proc())
    try:
        if running:
            send("save-off")
            send("save-all flush")
            time.sleep(6)
        (MC / "backups").mkdir(exist_ok=True)
        name = time.strftime("world-%Y%m%d-%H%M%S.tar.gz")
        with tarfile.open(MC / "backups" / name, "w:gz") as t:
            for w in ("world", "world_nether", "world_the_end"):
                if (MC / w).exists():
                    t.add(MC / w, arcname=w)
        for old in sorted((MC / "backups").glob("world-*.tar.gz"))[:-5]:
            old.unlink()
    finally:
        if running:
            send("save-on")
        busy["backup"] = False


def do_restart():
    send("stop")
    wait_stop()
    start_server()


def auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return jsonify(error="auth"), 401
        return fn(*a, **kw)
    return wrapper


# ---------- routes ----------
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.post("/api/login")
def login():
    pw = (request.get_json(silent=True) or {}).get("password", "")
    if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)
    time.sleep(1)
    return jsonify(error="bad password"), 403


@app.get("/api/status")
@auth
def status():
    p = java_proc()
    vm = psutil.virtual_memory()
    try:
        cpu = psutil.cpu_percent(interval=None)
    except Exception:
        cpu = None
    return jsonify(
        running=bool(p),
        uptime=int(time.time() - p.create_time()) if p else 0,
        java_mb=int(p.memory_info().rss / 1048576) if p else 0,
        ram_used_mb=int(vm.used / 1048576),
        ram_total_mb=int(vm.total / 1048576),
        cpu=cpu,
        temp=temperature(),
        battery=battery(),
        players=online_players() if p else [],
        backing_up=busy["backup"],
        backups=sorted(x.name for x in (MC / "backups").glob("world-*.tar.gz"))[-5:],
    )


@app.get("/api/log")
@auth
def log():
    return jsonify(lines=tail_lines(LOG, 200))


@app.post("/api/action")
@auth
def action():
    data = request.get_json(silent=True) or {}
    act = data.get("action")
    if act == "start":
        start_server()
    elif act == "stop":
        send("stop")
    elif act == "restart":
        threading.Thread(target=do_restart, daemon=True).start()
    elif act == "kill":
        run(["pkill", "-f", "paper.jar"])
    elif act == "backup":
        threading.Thread(target=do_backup, daemon=True).start()
    elif act in ("kick", "ban", "op"):
        name = data.get("player", "")
        if not NAME_RE.match(name):
            return jsonify(error="bad name"), 400
        send(f"{act} {name}")
    elif act == "cmd":
        cmd = str(data.get("cmd", "")).replace("\n", " ").replace("\r", " ").strip()
        if cmd:
            send(cmd.lstrip("/"))
    else:
        return jsonify(error="unknown action"), 400
    return jsonify(ok=True)


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f1115">
<title>MC Server</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262b36;--tx:#e8eaf0;--mute:#8b93a7;--ok:#3ddc84;--bad:#ff5c5c;--warn:#ffb020;--acc:#5b8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 system-ui,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:14px}
h1{font-size:18px;margin:0}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.pill{padding:4px 12px;border-radius:99px;font-size:13px;font-weight:600;background:var(--line)}
.pill.on{background:#12361f;color:var(--ok)}.pill.off{background:#3a1717;color:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
@media(min-width:600px){.grid{grid-template-columns:repeat(4,1fr)}}
.stat .k{color:var(--mute);font-size:12px}.stat .v{font-size:20px;font-weight:700}
.bar{height:6px;background:var(--line);border-radius:9px;margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);width:0}
.btns{display:flex;flex-wrap:wrap;gap:8px}
button{background:var(--line);color:var(--tx);border:0;border-radius:10px;padding:10px 14px;font:inherit;font-weight:600}
button:active{transform:scale(.97)}
.go{background:#1c5a34}.stop{background:#6b2a2a}.warn{background:#6b5314}
#con{background:#0a0c10;border-radius:10px;padding:10px;height:280px;overflow:auto;font:12px/1.45 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;margin:0}
.row{display:flex;gap:8px;margin-top:8px}
input{flex:1;min-width:0;background:#0a0c10;color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit}
.pl{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line)}
.pl:last-child{border:0}.pl button{padding:6px 10px;font-size:13px;margin-left:6px}
.mute{color:var(--mute)}
#login{max-width:340px;margin:20vh auto;text-align:center}
</style></head><body>
<div class="wrap">
<div id="login" class="card" hidden>
  <h1>Server dashboard</h1><p class="mute">Enter your password</p>
  <input id="pw" type="password" placeholder="Password" autocomplete="current-password">
  <div class="row"><button class="go" style="flex:1" id="lg">Log in</button></div>
  <p id="lerr" class="mute"></p>
</div>
<div id="app" hidden>
<header><h1>⛏ Minecraft Server</h1><span id="pill" class="pill off">Offline</span></header>
<div class="card"><div class="grid">
  <div class="stat"><div class="k">Players</div><div class="v" id="s-pl">0</div></div>
  <div class="stat"><div class="k">Uptime</div><div class="v" id="s-up">-</div></div>
  <div class="stat"><div class="k">Temp</div><div class="v" id="s-t">-</div></div>
  <div class="stat"><div class="k">Battery</div><div class="v" id="s-b">-</div></div>
  <div class="stat"><div class="k">Phone RAM</div><div class="v" id="s-r">-</div><div class="bar"><i id="b-r"></i></div></div>
  <div class="stat"><div class="k">Java RAM</div><div class="v" id="s-j">-</div></div>
  <div class="stat"><div class="k">CPU</div><div class="v" id="s-c">-</div><div class="bar"><i id="b-c"></i></div></div>
  <div class="stat"><div class="k">Backups</div><div class="v" id="s-bk">-</div></div>
</div></div>
<div class="card"><div class="btns">
  <button class="go" data-a="start">Start</button>
  <button data-a="restart">Restart</button>
  <button class="stop" data-a="stop">Stop</button>
  <button class="warn" data-a="kill">Force kill</button>
  <button data-a="backup">Backup world</button>
</div></div>
<div class="card"><b>Console</b>
  <pre id="con"></pre>
  <div class="row"><input id="cmd" placeholder="Type a command (no slash)"><button class="go" id="send">Send</button></div>
</div>
<div class="card"><b>Online players</b><div id="plist" class="mute">Nobody online</div></div>
</div></div>
<script>
const $=s=>document.querySelector(s);
async function api(path,body){
  const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  if(r.status===401){showLogin();throw new Error('auth')}
  return r.json();
}
function showLogin(){$('#login').hidden=false;$('#app').hidden=true}
async function login(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});
  if(r.ok){$('#login').hidden=true;$('#app').hidden=false;tick()}else{$('#lerr').textContent='Wrong password'}
}
$('#lg').onclick=login;$('#pw').onkeydown=e=>{if(e.key==='Enter')login()};
const fmtUp=s=>{if(!s)return'-';const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+'h '+m+'m':m+'m'};
async function status(){
  const s=await api('/api/status');
  $('#app').hidden=false;$('#login').hidden=true;
  const p=$('#pill');p.textContent=s.running?'Online':'Offline';p.className='pill '+(s.running?'on':'off');
  $('#s-pl').textContent=s.players.length;
  $('#s-up').textContent=fmtUp(s.uptime);
  $('#s-t').textContent=s.temp==null?'n/a':s.temp+'°C';
  $('#s-b').textContent=s.battery==null?'n/a':s.battery+'%';
  $('#s-r').textContent=s.ram_used_mb+' / '+s.ram_total_mb+' MB';
  $('#b-r').style.width=Math.min(100,s.ram_used_mb/s.ram_total_mb*100)+'%';
  $('#s-j').textContent=s.running?s.java_mb+' MB':'-';
  $('#s-c').textContent=s.cpu==null?'n/a':s.cpu+'%';
  $('#b-c').style.width=(s.cpu||0)+'%';
  $('#s-bk').textContent=s.backing_up?'saving...':s.backups.length+' saved';
  const box=$('#plist');box.textContent='';
  if(!s.players.length){box.textContent='Nobody online';box.className='mute'}
  else{box.className='';s.players.forEach(n=>{
    const row=document.createElement('div');row.className='pl';
    const nm=document.createElement('span');nm.textContent=n;row.appendChild(nm);
    const g=document.createElement('span');
    ['kick','ban','op'].forEach(a=>{const b=document.createElement('button');b.textContent=a;
      b.onclick=()=>{if(confirm(a+' '+n+'?'))api('/api/action',{action:a,player:n})};g.appendChild(b)});
    row.appendChild(g);box.appendChild(row)})}
}
async function logs(){
  const d=await api('/api/log');const c=$('#con');
  const near=c.scrollHeight-c.scrollTop-c.clientHeight<60;
  c.textContent=d.lines.join('\n');if(near)c.scrollTop=c.scrollHeight;
}
async function tick(){try{await status();await logs()}catch(e){}}
document.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
  const a=b.dataset.a;
  if((a==='stop'||a==='kill'||a==='restart')&&!confirm(a+' the server?'))return;
  await api('/api/action',{action:a});setTimeout(tick,800)});
async function sendCmd(){const i=$('#cmd');if(!i.value.trim())return;await api('/api/action',{action:'cmd',cmd:i.value});i.value='';setTimeout(tick,500)}
$('#send').onclick=sendCmd;$('#cmd').onkeydown=e=>{if(e.key==='Enter')sendCmd()};
tick();setInterval(tick,2500);
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
