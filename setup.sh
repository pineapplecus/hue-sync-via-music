#!/usr/bin/env bash
# One-command setup for hue-music-sync on a clean Mac. Safe to re-run: every step checks first.
#
#   ./setup.sh            full setup (asks before changing audio output or touching the bridge)
#   ./setup.sh --undo     put the Mac's audio output back to normal and remove the virtual device
set -euo pipefail
cd "$(dirname "$0")"

bold() { printf "\n\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "\n\033[31mError:\033[0m %s\n" "$1" >&2; exit 1; }
ask()  { local a; read -r -p "  $1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

[[ "$(uname)" == "Darwin" ]] || die "This setup is for macOS only (it uses CoreAudio and BlackHole)."

# ---------------------------------------------------------------- undo
if [[ "${1:-}" == "--undo" ]]; then
  bold "Restoring normal audio output"
  if [[ ! -x bin/setup_audio ]]; then swiftc -O -swift-version 5 setup_audio.swift -o bin/setup_audio 2>/dev/null; fi
  ./bin/setup_audio remove
  exit 0
fi

# ---------------------------------------------------------------- 1. command line tools
bold "1/7  Apple command line tools (needed to build the small native helpers)"
if ! xcode-select -p >/dev/null 2>&1 || ! command -v swiftc >/dev/null 2>&1; then
  warn "Not installed. A system dialog will open: click Install, wait for it to finish, then run ./setup.sh again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi
ok "found ($(swiftc --version 2>&1 | head -1 | cut -c1-60))"

# ---------------------------------------------------------------- 2. Python
bold "2/7  Python"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
[[ -n "$PY" ]] || die "Python 3.9+ not found. The command line tools (step 1) include one; re-run after they finish."
ok "using $PY ($($PY --version))"

# ---------------------------------------------------------------- 3. Homebrew
bold "3/7  Homebrew (package manager; installs the BlackHole audio driver)"
load_brew() {
  if [[ -x /opt/homebrew/bin/brew ]]; then eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then eval "$(/usr/local/bin/brew shellenv)"; fi
}
load_brew
if command -v brew >/dev/null 2>&1; then
  ok "already installed ($(brew --version | head -1))"
else
  warn "Homebrew is not installed. Installing it now (official installer from brew.sh, takes a few minutes)."
  warn "It needs your Mac password: type it when asked (nothing shows as you type)."
  sudo -v || die "Homebrew needs administrator access. Run ./setup.sh again and enter your password."
  # Keep the sudo ticket alive while the installer runs, then stop the keep-alive.
  ( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &
  SUDO_KEEPALIVE=$!
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || { kill "$SUDO_KEEPALIVE" 2>/dev/null; die "Homebrew install failed. See https://brew.sh for manual steps, then re-run ./setup.sh"; }
  kill "$SUDO_KEEPALIVE" 2>/dev/null || true
  load_brew
  command -v brew >/dev/null 2>&1 || die "Homebrew installed but 'brew' is not on the PATH. Open a new terminal and run ./setup.sh again."
  # Make `brew` available in future terminals too (the installer only prints this instruction).
  BREW_BIN="$(command -v brew)"
  grep -qs "brew shellenv" "$HOME/.zprofile" 2>/dev/null || echo "eval \"\$($BREW_BIN shellenv)\"" >> "$HOME/.zprofile"
  ok "installed ($(brew --version | head -1))"
fi

# ---------------------------------------------------------------- 4. BlackHole
bold "4/7  BlackHole (virtual audio device that lets the app hear what the Mac plays)"
if [[ -d /Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver ]]; then
  ok "already installed"
else
  brew install blackhole-2ch
  warn "Restarting the Mac's audio service so the driver loads (asks for your password; sound cuts out for a second)."
  sudo killall coreaudiod || true
fi

# ---------------------------------------------------------------- 5. Python packages + helpers
bold "5/7  Python packages and native helpers"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
ok "python packages installed in .venv"
mkdir -p bin
swiftc -O -swift-version 5 hue_stream.swift -o bin/hue_stream 2>/dev/null && ok "built bin/hue_stream (Hue streaming helper)"
swiftc -O -swift-version 5 setup_audio.swift -o bin/setup_audio 2>/dev/null && ok "built bin/setup_audio"

echo "  waiting for BlackHole to appear..."
for _ in $(seq 1 15); do
  if .venv/bin/python -c "import sounddevice as sd,sys; sys.exit(0 if any('blackhole' in d['name'].lower() for d in sd.query_devices()) else 1)" 2>/dev/null; then break; fi
  sleep 1
done
.venv/bin/python -c "import sounddevice as sd,sys; sys.exit(0 if any('blackhole' in d['name'].lower() for d in sd.query_devices()) else 1)" \
  || die "BlackHole is installed but not visible yet. Log out and back in (or reboot), then run ./setup.sh again."
ok "BlackHole is visible"

# ---------------------------------------------------------------- 6. audio routing + mic permission
bold "6/7  Audio routing"
echo "  This creates a 'Hue Sync Output' device that plays to your current speakers AND feeds the app,"
echo "  and makes it your Mac's output. (Undo any time with: ./setup.sh --undo)"
if ask "Set that up now?"; then
  ./bin/setup_audio
  warn "Macs can't change the volume of this output with the keyboard keys; set volume on your speakers/app instead."
fi
echo "  Triggering the microphone permission prompt (needed to read BlackHole)..."
.venv/bin/python -c "import sounddevice as sd; sd.rec(4410, samplerate=44100, channels=1, device=[i for i,d in enumerate(sd.query_devices()) if 'blackhole' in d['name'].lower()][0]); sd.wait()" 2>/dev/null || true
warn "If macOS asked for microphone access, click Allow. If you clicked Don't Allow (or no prompt appeared and"
warn "the lights never react), enable your terminal app in System Settings → Privacy & Security → Microphone,"
warn "then fully quit and reopen the terminal."

# ---------------------------------------------------------------- 7. Hue bridge
bold "7/7  Hue bridge"
echo "  Make sure this Mac is on the same Wi-Fi/network as your Hue Bridge."
if [[ -f "$HOME/.hue-music-sync.json" ]] && grep -q clientkey "$HOME/.hue-music-sync.json" 2>/dev/null; then
  ok "already paired (delete ~/.hue-music-sync.json and re-run to pair again)"
elif ask "Pair with your Hue Bridge now? You'll need to press the big button on the bridge when asked."; then
  .venv/bin/python sync.py pair
fi
if [[ -f "$HOME/.hue-music-sync.json" ]] && grep -q clientkey "$HOME/.hue-music-sync.json" 2>/dev/null; then
  if [[ -z "$(.venv/bin/python sync.py areas 2>/dev/null)" ]]; then
    ask "No Entertainment Area yet. Create one with all your color lights? (appears in the Hue app as 'Party Sync')" \
      && .venv/bin/python sync.py setup-area
  else
    ok "Entertainment Area found: $(.venv/bin/python sync.py areas | head -1)"
  fi
  if ask "Run a 8-second light test now (red, green, blue, white; no music needed)?"; then
    .venv/bin/python sync.py stream-test || warn "The test failed. See the message above; CLAUDE.md has a troubleshooting section."
  fi
fi

bold "All set"
cat <<'EOF'
  Play music on this Mac (Spotify, Music, a browser...), then:

      ./start.sh                         Melissa's pink / lavender / champagne pulse
      ./start.sh --preset noe-default    red pulses + white strobes on deep bass
      ./start.sh --every 2               calmer: pulse on every second beat

  Ctrl+C stops it and returns the lights to warm white.
  Using a DJ mixer instead? Plug it into a line-in/USB interface and pass --device "<name>" (see: .venv/bin/python sync.py devices)
EOF
