#!/bin/bash
# mailbrief installer — macOS / Apple Silicon.
#
# Idempotent: safe to re-run. Detects what is already present rather than
# assuming a clean machine, and never overwrites your resume, boards list,
# or state database.
#
#   bash install.sh
#
# Installs to ~/.mailbrief. Touches nothing else except (optionally) a
# LaunchAgent plist, which is written but NOT loaded -- see the end.

set -euo pipefail

MAILBRIEF_HOME="$HOME/.mailbrief"
MODEL="${MAILBRIEF_MODEL:-qwen3:14b}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[[ "$(uname -s)" == "Darwin" ]] || die "macOS only."
[[ "$(uname -m)" == "arm64"  ]] || warn "Not Apple Silicon; model sizing below assumes it."

[[ -f "$SRC_DIR/mailbrief.py" ]] || die "mailbrief.py not found next to this script."

RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
say "Detected ${RAM_GB} GB unified memory."
if   (( RAM_GB <= 16 )); then SUGGEST="qwen3:8b"
elif (( RAM_GB <= 32 )); then SUGGEST="qwen3:14b"
else                          SUGGEST="qwen3:30b-a3b"
fi
[[ "$MODEL" == "$SUGGEST" ]] || warn "Installing '$MODEL'; '$SUGGEST' is the better fit for ${RAM_GB} GB."

# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------

# Homebrew lives in a non-standard prefix on some setups; check several.
for brew_path in /opt/homebrew/bin/brew "$HOME/.homebrew/bin/brew" /usr/local/bin/brew; do
    [[ -x "$brew_path" ]] && { eval "$("$brew_path" shellenv)"; break; }
done

if command -v ollama >/dev/null 2>&1; then
    say "Ollama already installed: $(command -v ollama)"
else
    command -v brew >/dev/null 2>&1 \
        || die "Neither ollama nor brew found. Install Ollama from https://ollama.com/download and re-run."
    say "Installing Ollama via Homebrew..."
    brew install ollama
fi

# Start the server if it is not already answering.
if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    say "Starting Ollama..."
    if command -v brew >/dev/null 2>&1 && brew services list 2>/dev/null | grep -q '^ollama'; then
        brew services start ollama
    else
        nohup ollama serve >/dev/null 2>&1 &
    fi
    for _ in $(seq 1 30); do
        curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
        sleep 1
    done
fi
curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
    || die "Ollama is not responding on :11434. Start it manually with 'ollama serve' and re-run."
say "Ollama is up."

if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
    say "Model $MODEL already pulled."
else
    say "Pulling $MODEL (several GB; this is the slow step)..."
    ollama pull "$MODEL"
fi

# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

say "Installing to $MAILBRIEF_HOME"
mkdir -p "$MAILBRIEF_HOME"/{briefs,drafts}
cp "$SRC_DIR/mailbrief.py" "$MAILBRIEF_HOME/mailbrief.py"
chmod +x "$MAILBRIEF_HOME/mailbrief.py"

# macOS ships Python 3.9 as /usr/bin/python3. The code supports it, but prefer
# a newer interpreter when one is present rather than silently pinning a venv
# to the oldest thing on PATH.
PYTHON=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    PYTHON="$(command -v "$cand")"
    break
done
[[ -n "$PYTHON" ]] || die "No python3 on PATH."
PYV="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Using Python $PYV ($PYTHON)"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
    || die "Python 3.9+ required; found $PYV."

if [[ ! -d "$MAILBRIEF_HOME/venv" ]]; then
    say "Creating virtualenv..."
    "$PYTHON" -m venv "$MAILBRIEF_HOME/venv"
else
    EXISTING="$("$MAILBRIEF_HOME/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    say "Reusing existing virtualenv (Python $EXISTING)"
    [[ "$EXISTING" == "$PYV" ]] || warn "venv is on $EXISTING while $PYV is available; 'rm -rf $MAILBRIEF_HOME/venv' and re-run to rebuild."
fi
say "Installing Python dependencies..."
"$MAILBRIEF_HOME/venv/bin/pip" install --quiet --upgrade pip
"$MAILBRIEF_HOME/venv/bin/pip" install --quiet httpx imap-tools

# Creates state.db, boards.json and an empty resume.txt. Never clobbers
# existing ones -- mailbrief.py guards each with an existence check.
"$MAILBRIEF_HOME/venv/bin/python" "$MAILBRIEF_HOME/mailbrief.py" init

# --------------------------------------------------------------------------
# LaunchAgent
#
# Generated here rather than shipped as a template, so the absolute paths are
# correct for this machine. launchd does not expand ~ and does not source your
# shell profile, so every path must be literal.
# --------------------------------------------------------------------------

PLIST="$HOME/Library/LaunchAgents/com.user.mailbrief.plist"
mkdir -p "$HOME/Library/LaunchAgents"

read -r -p "Email address for IMAP (blank to skip scheduling): " IMAP_ADDR || true

if [[ -n "${IMAP_ADDR:-}" ]]; then
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.mailbrief</string>
    <key>ProgramArguments</key>
    <array>
        <string>${MAILBRIEF_HOME}/venv/bin/python</string>
        <string>${MAILBRIEF_HOME}/mailbrief.py</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>IMAP_USER</key><string>${IMAP_ADDR}</string>
        <key>IMAP_HOST</key><string>imap.gmail.com</string>
        <key>MAILBRIEF_MODEL</key><string>${MODEL}</string>
        <key>OLLAMA_URL</key><string>http://127.0.0.1:11434</string>
        <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${HOME}/.homebrew/bin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>15</integer></dict>
        <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>30</integer></dict>
    </array>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>${MAILBRIEF_HOME}/run.log</string>
    <key>StandardErrorPath</key><string>${MAILBRIEF_HOME}/error.log</string>
    <key>ThrottleInterval</key><integer>300</integer>
</dict>
</plist>
PLIST_EOF
    plutil -lint "$PLIST" >/dev/null || die "Generated plist failed validation."
    say "LaunchAgent written to $PLIST (not loaded yet)."
else
    IMAP_ADDR="you@example.com"
    warn "Skipped LaunchAgent. Re-run this script later to schedule it."
fi

# --------------------------------------------------------------------------

cat <<EOF

$(say "Installed.")

Two steps remain. Both need you, not an automation:

  1. Gmail app password. Requires 2FA on the account. Generate one at
     https://myaccount.google.com/apppasswords  then store it:

       security add-generic-password -s mailbrief -a ${IMAP_ADDR} -w

     The -w flag prompts; the password is not echoed and does not enter
     your shell history. Do not paste this secret into a chat window.

  2. Your resume, plain text, for posting relevance scoring:

       \$EDITOR ${MAILBRIEF_HOME}/resume.txt
       \$EDITOR ${MAILBRIEF_HOME}/boards.json    # companies to watch

Then verify end to end:

  export IMAP_USER=${IMAP_ADDR}
  ${MAILBRIEF_HOME}/venv/bin/python ${MAILBRIEF_HOME}/mailbrief.py triage

Once that produces a brief you like, enable the twice-daily schedule:

  launchctl load ~/Library/LaunchAgents/com.user.mailbrief.plist

Leave it unloaded until the manual run works, or it will fail silently
into ${MAILBRIEF_HOME}/error.log twice a day.
EOF
