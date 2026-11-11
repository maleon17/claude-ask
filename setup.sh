#!/usr/bin/env bash
# Installer for the ClaudeAsk backend: the shared queue relay (cmd_queue.py)
# and the Claude Code worker (claude_watcher.py).
#
# Safe to re-run: existing jarvis.env keys are kept, running services are left
# running, and a token added for another instance takes effect without a relay
# restart.
#
# Every question can be answered through the environment instead, which makes
# an unattended install possible:
#   JARVIS_TELEGRAM_MCP_PATH  CodexAsk's telegram_actions_mcp.py
#   JARVIS_OWNER_NAME, JARVIS_OWNER_TG_ID, JARVIS_NOMATS_CHAT_IDS, JARVIS_PERSONA_DIR
#   JARVIS_INSTANCE_ID        instance to register with the relay
#   JARVIS_RELAY_CONFIG_DIR   token/owner maps   (default ~/.config/telegram-ai)
#   JARVIS_STATE_DIR          queues and uploads (default ~/.local/state/telegram-ai)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found"; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# ask VAR "prompt" [default] -- a value already in the environment wins, then
# an interactive answer, then the default.
ask() {
    local variable="$1" prompt="$2" fallback="${3:-}" value="${!1:-}"
    if [[ -z "$value" && -t 0 ]]; then read -r -p "$prompt" value || value=""; fi
    printf -v "$variable" '%s' "${value:-$fallback}"
}
# env_file_set FILE KEY VALUE -- replace one key, keep every other line.
env_file_set() {
    python3 - "$1" "$2" "$3" <<'PY'
import os, sys
path, key, value = sys.argv[1:4]
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
lines = [line for line in lines if not line.startswith(key + "=")]
lines.append(f"{key}={value}")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PY
}
env_file_get() { [[ -f "$1" ]] && sed -n "s/^$2=//p" "$1" | tail -n 1 || true; }
# The value of one variable in a running service's environment, if readable.
running_env() {
    local pid
    pid="$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)"
    [[ -n "$pid" && "$pid" != 0 && -r "/proc/$pid/environ" ]] || return 0
    tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$2=//p" | tail -n 1
}

echo "== Claude Jarvis setup =="
need python3
need systemctl
[[ $EUID -eq 0 ]] || need sudo
python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
    || die "python3 venv support is missing (Debian/Ubuntu: sudo apt install python3-venv)"

CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
[[ -n "$CLAUDE_BIN" ]] || die "Claude Code CLI not found; install it: curl -fsSL https://claude.ai/install.sh | bash"
"$CLAUDE_BIN" auth status >/dev/null 2>&1 || die "Claude Code is not logged in; run: $CLAUDE_BIN auth login --claudeai"
echo "Claude account: authenticated ($CLAUDE_BIN)"

# --- Telegram-action MCP server (lives in the CodexAsk checkout) ------------
# Checked before anything is installed, so a missing checkout fails fast.
DEFAULT_MCP_PATH="${JARVIS_TELEGRAM_MCP_PATH:-}"
if [[ -z "$DEFAULT_MCP_PATH" ]]; then
    for candidate in "$ROOT/../codex-ask" "$ROOT/../codex-jarvis" "$HOME/codex-ask" "$HOME/codex-jarvis"; do
        if [[ -f "$candidate/telegram_actions_mcp.py" ]]; then
            DEFAULT_MCP_PATH="$(cd "$candidate" && pwd)/telegram_actions_mcp.py"
            break
        fi
    done
fi
ask JARVIS_TELEGRAM_MCP_PATH "CodexAsk telegram_actions_mcp.py [${DEFAULT_MCP_PATH:-not found}]: " "$DEFAULT_MCP_PATH"
if [[ ! -f "$JARVIS_TELEGRAM_MCP_PATH" ]]; then
    die "CodexAsk's telegram_actions_mcp.py not found${JARVIS_TELEGRAM_MCP_PATH:+ at $JARVIS_TELEGRAM_MCP_PATH}.
ClaudeAsk uses the Telegram-action MCP server shipped with CodexAsk. Clone it
next to this checkout and re-run:
  git clone https://github.com/maleon17/codex-ask.git \"$(dirname "$ROOT")/codex-ask\"
(or point JARVIS_TELEGRAM_MCP_PATH at an existing copy)."
fi
JARVIS_TELEGRAM_MCP_PATH="$(cd "$(dirname "$JARVIS_TELEGRAM_MCP_PATH")" && pwd)/$(basename "$JARVIS_TELEGRAM_MCP_PATH")"

MCP_VENV="${CLAUDE_JARVIS_MCP_VENV:-$ROOT/.venv}"
VENV_PYTHON="$MCP_VENV/bin/python"
echo "Installing Python dependencies into $MCP_VENV"
python3 -m venv "$MCP_VENV"
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r requirements.txt -r requirements-mcp.txt

sed \
    -e "s|__MCP_PYTHON__|$VENV_PYTHON|g" \
    -e "s|__INSTALL_DIR__|$ROOT|g" \
    mcp_telegram_tools_config.json.example > mcp_telegram_tools_config.json
mkdir -p "$ROOT/runtime"
chmod 700 "$ROOT/runtime"

python3 -m py_compile claude_watcher.py cmd_queue.py mcp_telegram_tools.py remote_terminal.py
echo "Prepared local MCP config: $ROOT/mcp_telegram_tools_config.json"

# --- Jarvis persona: profanity notice + env (owner + softening list) ------
cat <<'NOTE'

Note: the Jarvis persona ships with profanity and dark humour enabled by
default - a deliberately informal, entertainment-leaning bot. To tone that
down, edit the persona after setup with the  .persona  command in Telegram
(CodexAsk: .xpersona), or edit personas/<instance>.md directly (re-read
automatically on change, no restart).
NOTE

_jarvis_clean() {  # drop control chars + shell/systemd-hazardous chars, keep UTF-8 letters
    python3 - "$1" <<'PY'
import sys
bad = set('\\"\'`$')
s = sys.argv[1] if len(sys.argv) > 1 else ""
sys.stdout.write("".join(c for c in s if c >= " " and c != "\x7f" and c not in bad))
PY
}

JARVIS_ENV="$ROOT/jarvis.env"
PREV_OWNER_NAME="$(env_file_get "$JARVIS_ENV" JARVIS_OWNER_NAME)"
PREV_OWNER_TG_ID="$(env_file_get "$JARVIS_ENV" JARVIS_OWNER_TG_ID)"
PREV_NOMATS="$(env_file_get "$JARVIS_ENV" JARVIS_NOMATS_CHAT_IDS)"
PREV_PERSONA_DIR="$(env_file_get "$JARVIS_ENV" JARVIS_PERSONA_DIR)"
ask JARVIS_OWNER_NAME "Owner display name for the persona template [${PREV_OWNER_NAME:-skip}]: " "$PREV_OWNER_NAME"
ask JARVIS_OWNER_TG_ID "Owner Telegram numeric id (message @userinfobot to get it) [${PREV_OWNER_TG_ID:-skip}]: " "$PREV_OWNER_TG_ID"
ask JARVIS_NOMATS_CHAT_IDS "Chat ids where profanity is suppressed, comma-separated [${PREV_NOMATS:-none}]: " "$PREV_NOMATS"
ask JARVIS_PERSONA_DIR "Persona directory [${PREV_PERSONA_DIR:-$ROOT/personas}]: " "${PREV_PERSONA_DIR:-$ROOT/personas}"
J_OWNER_NAME="$(_jarvis_clean "$JARVIS_OWNER_NAME")"
J_OWNER_TG_ID="$(printf '%s' "$JARVIS_OWNER_TG_ID" | tr -cd '0-9')"
J_NOMATS="$(printf '%s' "$JARVIS_NOMATS_CHAT_IDS" | tr -cd '0-9,-')"
J_PERSONA_DIR="$(_jarvis_clean "$JARVIS_PERSONA_DIR")"
if [[ "$J_PERSONA_DIR" != "$ROOT/personas" ]]; then
    mkdir -p "$J_PERSONA_DIR"
    [[ -f "$J_PERSONA_DIR/default.md.example" ]] || \
        cp "$ROOT/personas/default.md.example" "$J_PERSONA_DIR/default.md.example"
fi
[[ -n "$J_OWNER_NAME" ]]  && env_file_set "$JARVIS_ENV" JARVIS_OWNER_NAME "$J_OWNER_NAME"
[[ -n "$J_OWNER_TG_ID" ]] && env_file_set "$JARVIS_ENV" JARVIS_OWNER_TG_ID "$J_OWNER_TG_ID"
env_file_set "$JARVIS_ENV" JARVIS_NOMATS_CHAT_IDS "$J_NOMATS"
env_file_set "$JARVIS_ENV" JARVIS_PERSONA_DIR "$J_PERSONA_DIR"
chmod 600 "$JARVIS_ENV"
echo "Wrote $JARVIS_ENV (git-ignored; both services load it via EnvironmentFile=)."

# --- Relay authentication + systemd services --------------------------------
# The queue relay (cmd_queue.py) and the watcher (claude_watcher.py) are
# shared infrastructure: one instance of each serves every userbot instance
# that authenticates against them. A second run of this script (e.g. to add
# another instance_id) must not reinstall or restart either if they are
# already serving traffic.
QUEUE_SERVICE_NAME="${CLAUDE_JARVIS_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"
WATCHER_SERVICE_NAME="${CLAUDE_JARVIS_WATCHER_SERVICE:-jarvis-ask-watcher.service}"

QUEUE_ACTIVE=0
WATCHER_ACTIVE=0
systemctl is-active --quiet "$QUEUE_SERVICE_NAME" 2>/dev/null && QUEUE_ACTIVE=1
systemctl is-active --quiet "$WATCHER_SERVICE_NAME" 2>/dev/null && WATCHER_ACTIVE=1

# A relay that is already running is the source of truth for where its
# credentials and queues live; a new token written anywhere else is invisible
# to it.
if (( QUEUE_ACTIVE )); then
    for name in JARVIS_RELAY_CONFIG_DIR JARVIS_RELAY_TOKENS_FILE JARVIS_RELAY_OWNERS_FILE JARVIS_STATE_DIR; do
        live_value="$(running_env "$QUEUE_SERVICE_NAME" "$name")"
        [[ -n "$live_value" ]] && export "$name=$live_value"
    done
fi
RELAY_CONFIG_DIR="${JARVIS_RELAY_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/telegram-ai}"
STATE_DIR="${JARVIS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/telegram-ai}"
export JARVIS_RELAY_CONFIG_DIR="$RELAY_CONFIG_DIR" JARVIS_STATE_DIR="$STATE_DIR"
mkdir -p "$RELAY_CONFIG_DIR" "$STATE_DIR"
chmod 700 "$RELAY_CONFIG_DIR" "$STATE_DIR"
WORKER_TOKEN_ENV="$RELAY_CONFIG_DIR/relay-token-claude.env"

echo
echo "== Relay authentication =="
RELAY_TOKEN=""
ask JARVIS_INSTANCE_ID "Instance ID for this ClaudeAsk deployment (e.g. mybot; blank to configure later): " ""
INSTANCE_ID="$JARVIS_INSTANCE_ID"
if [[ -n "$INSTANCE_ID" ]]; then
    [[ "$INSTANCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || die "invalid instance id: $INSTANCE_ID"
    ask JARVIS_INSTANCE_OWNER_TG_ID "Owner Telegram numeric ID for instance '$INSTANCE_ID'${J_OWNER_TG_ID:+ [$J_OWNER_TG_ID]}: " "$J_OWNER_TG_ID"
    OWNER_TG_ID="$(printf '%s' "$JARVIS_INSTANCE_OWNER_TG_ID" | tr -cd '0-9')"
    [[ -n "$OWNER_TG_ID" ]] || die "owner Telegram ID is required to authorize instance '$INSTANCE_ID'"

    # The relay's own interpreter: cmd_queue.py imports packages that exist
    # only in the virtualenv, and the maps must stay owned by the relay user.
    "$VENV_PYTHON" "$ROOT/cmd_queue.py" --set-owner "$INSTANCE_ID" "$OWNER_TG_ID"
    RELAY_TOKEN="$("$VENV_PYTHON" "$ROOT/cmd_queue.py" --add-token "$INSTANCE_ID" --worker-env "$WORKER_TOKEN_ENV")"
    echo "Registered owner $OWNER_TG_ID and a new token for instance '$INSTANCE_ID' in $RELAY_CONFIG_DIR"
    echo "(a running relay picks this up by itself; no restart needed)."
    if (( WATCHER_ACTIVE )); then
        echo "NOTE: $WATCHER_SERVICE_NAME is already running and reads $WORKER_TOKEN_ENV"
        echo "only at start. Restart it once no .ask is in flight:"
        echo "  sudo systemctl restart $WATCHER_SERVICE_NAME"
    fi
else
    echo "No instance ID given; skipping relay token generation."
    echo "Re-run setup.sh once you're ready, or see 'Relay authentication and trust"
    echo "boundary' in the README to do it by hand."
fi

echo
echo "== systemd services =="
render_unit() {  # render_unit TEMPLATE -> path of the rendered temp file
    local rendered
    rendered="$(mktemp)"
    sed \
        -e "s|__USER__|$(id -un)|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__INSTALL_DIR__|$ROOT|g" \
        -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
        -e "s|__CLAUDE_BIN__|$CLAUDE_BIN|g" \
        -e "s|__RELAY_CONFIG_DIR__|$RELAY_CONFIG_DIR|g" \
        -e "s|__STATE_DIR__|$STATE_DIR|g" \
        -e "s|__JARVIS_TELEGRAM_MCP_PATH__|$JARVIS_TELEGRAM_MCP_PATH|g" \
        "$1" > "$rendered"
    printf '%s' "$rendered"
}
if (( QUEUE_ACTIVE )); then
    echo "$QUEUE_SERVICE_NAME is already active; reusing it (not reinstalling the relay)."
else
    UNIT_FILE="$(render_unit claude-jarvis-queue.service.example)"
    echo "Installing /etc/systemd/system/$QUEUE_SERVICE_NAME (sudo required)"
    as_root install -m 0644 "$UNIT_FILE" "/etc/systemd/system/$QUEUE_SERVICE_NAME"
    rm -f "$UNIT_FILE"
    as_root systemctl daemon-reload
    if [[ -n "$INSTANCE_ID" ]]; then
        as_root systemctl enable --now "$QUEUE_SERVICE_NAME"
    else
        # The relay refuses to start without at least one registered instance.
        as_root systemctl enable "$QUEUE_SERVICE_NAME"
        echo "$QUEUE_SERVICE_NAME is installed but not started: no instance is registered yet."
    fi
fi

if (( WATCHER_ACTIVE )); then
    echo "$WATCHER_SERVICE_NAME is already active; leaving it running."
else
    UNIT_FILE="$(render_unit claude-jarvis-watcher.service.example)"
    echo "Installing /etc/systemd/system/$WATCHER_SERVICE_NAME (sudo required)"
    as_root install -m 0644 "$UNIT_FILE" "/etc/systemd/system/$WATCHER_SERVICE_NAME"
    rm -f "$UNIT_FILE"
    as_root systemctl daemon-reload
    as_root systemctl enable --now "$WATCHER_SERVICE_NAME"
fi

echo
as_root systemctl --no-pager --full status "$QUEUE_SERVICE_NAME" "$WATCHER_SERVICE_NAME" || true
echo
if [[ -n "$INSTANCE_ID" && -n "$RELAY_TOKEN" ]]; then
    echo "Backend ready. Upload claude_ask.py to the Telethon userbot, reply .lm, then run"
    echo "(in a private chat - the token is shown once and stored nowhere in plaintext"
    echo "except this worker's own $WORKER_TOKEN_ENV):"
    echo "  .asknet token $RELAY_TOKEN"
    echo "  .asknet local $INSTANCE_ID"
else
    echo "Backend installed. Re-run setup.sh with an instance ID to register one, then"
    echo "upload claude_ask.py to the Telethon userbot, reply .lm and configure .asknet."
fi

# --- optional: graphify code-map -------------------------------------------
# graphify (PyPI package "graphifyy", github.com/Graphify-Labs/graphify) turns
# this repo into a queryable knowledge graph under graphify-out/. Optional.
setup_graphify() {
    local platform="$1"
    if command -v graphify >/dev/null 2>&1; then
        echo "graphify: already on PATH ($(command -v graphify))"
    elif command -v uv >/dev/null 2>&1; then
        uv tool install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    elif command -v pipx >/dev/null 2>&1; then
        pipx install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    else
        echo "graphify: needs 'uv' or 'pipx' to install - skipping"
        return 0
    fi
    graphify install --platform "$platform" >/dev/null 2>&1 || true
    graphify update . >/dev/null 2>&1 || true
    echo "graphify: code map built under graphify-out/ (re-run 'graphify update .' after edits;"
    echo "          a post-commit hook keeps it fresh if graphify installed one)"
}

if [ -t 0 ]; then
    read -rp "Set up the graphify code-map for this repo? [y/N] " _SETUP_GRAPHIFY || _SETUP_GRAPHIFY=""
    case "${_SETUP_GRAPHIFY:-}" in
        [Yy]*) setup_graphify "claude" ;;
    esac
fi

bash "$ROOT/scripts/setup_userbot_network.sh" claude_ask.py
