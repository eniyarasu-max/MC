#!/usr/bin/env bash
# Minecraft server installer for UserLAnd (Ubuntu) on a phone.
# Usage:  bash install.sh [RAM_MB]      e.g.  bash install.sh 1536
set -e

RAM_MB="${1:-1536}"
MC_DIR="$HOME/mc"
HERE="$(cd "$(dirname "$0")" && pwd)"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "==> Installing packages (this takes a few minutes)..."
$SUDO apt update
$SUDO apt install -y screen curl wget unzip nano python3 python3-flask python3-psutil

echo "==> Installing Java (newest available: 25, then 21, then 17)..."
for v in 25 21 17; do
  if $SUDO apt install -y "openjdk-$v-jre-headless"; then
    echo "Installed Java $v"
    break
  fi
done
java -version

mkdir -p "$MC_DIR/plugins" "$MC_DIR/backups"
cd "$MC_DIR"

# ---------- small download helper ----------
cat > "$MC_DIR/dl.py" <<'PY'
import json, sys, urllib.request, urllib.parse
UA = {"User-Agent": "mc-phone-server/1.0 (personal use)"}

def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60)

def save(url, dest):
    with get(url) as r, open(dest, "wb") as f:
        while True:
            b = r.read(65536)
            if not b:
                break
            f.write(b)

kind, dest = sys.argv[1], sys.argv[-1]
if kind == "paper":
    base = "https://fill.papermc.io/v3/projects/paper"
    groups = json.load(get(base))["versions"]  # newest group first, newest version first
    for group in groups.values():
        for ver in group:
            builds = json.load(get(f"{base}/versions/{ver}/builds"))
            stable = [b for b in builds if b.get("channel") == "STABLE"]
            if stable:
                print("Paper version", ver)
                save(stable[0]["downloads"]["server:default"]["url"], dest)
                sys.exit(0)
    sys.exit("No stable Paper build found")
elif kind == "url":
    save(sys.argv[2], dest)
elif kind == "modrinth":
    slug = sys.argv[2]
    q = urllib.parse.quote('["paper"]')
    versions = json.load(get(f"https://api.modrinth.com/v2/project/{slug}/version?loaders={q}"))
    save(versions[0]["files"][0]["url"], dest)
PY

# ---------- Paper ----------
if [ ! -f paper.jar ]; then
  echo "==> Downloading Paper..."
  if [ -n "$PAPER_URL" ]; then
    python3 dl.py url "$PAPER_URL" paper.jar
  elif ! python3 dl.py paper paper.jar; then
    echo "Automatic Paper download failed."
    read -rp "Paste the Paper .jar download link from papermc.io: " PU
    python3 dl.py url "$PU" paper.jar
  fi
fi

# ---------- Bedrock support + helpers ----------
echo "==> Downloading plugins (best effort)..."
python3 dl.py url "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot" plugins/Geyser-Spigot.jar \
  || echo "!! Geyser download failed - get it from geysermc.org/download"
python3 dl.py url "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot" plugins/floodgate-spigot.jar \
  || echo "!! Floodgate download failed - get it from geysermc.org/download"
python3 dl.py modrinth coreprotect plugins/CoreProtect.jar \
  || echo "!! CoreProtect download failed - get it from modrinth.com/plugin/coreprotect (grief logging)"
python3 dl.py modrinth chunky plugins/Chunky.jar \
  || echo "!! Chunky download failed - get it from modrinth.com/plugin/chunky (pre-generates the world)"

# ---------- Server config ----------
echo "$RAM_MB" > ram.txt
echo "eula=true" > eula.txt

read -rp "World seed (press Enter for a random one): " SEED
if [ ! -f server.properties ]; then
cat > server.properties <<EOF
motd=Survival Server (Java + Bedrock)
gamemode=survival
difficulty=easy
pvp=true
max-players=8
view-distance=5
simulation-distance=4
spawn-protection=16
white-list=false
online-mode=true
level-seed=$SEED
server-port=25565
EOF
fi

# ---------- start script ----------
cat > start.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
RAM=$(cat ram.txt 2>/dev/null || echo 1536)
exec java -Xms${RAM}M -Xmx${RAM}M \
  -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 \
  -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch \
  -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M \
  -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 \
  -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 \
  -XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem \
  -XX:MaxTenuringThreshold=1 -jar paper.jar --nogui
EOF
chmod +x start.sh

# ---------- dashboard ----------
cp "$HERE/app.py" "$MC_DIR/app.py"
if [ ! -f dash_pass.txt ]; then
  read -rsp "Choose a dashboard password: " DP
  echo
  echo "$DP" > dash_pass.txt
  chmod 600 dash_pass.txt
fi

cat > dashboard.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
exec python3 app.py
EOF
chmod +x dashboard.sh

echo
echo "Done. Start the dashboard with:   cd ~/mc && ./dashboard.sh"
echo "Then open  http://<phone-ip>:8080  in a browser and press Start."
