#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found"; }

echo "== Claude Jarvis setup =="
need python3
need claude
need systemctl
need sudo

claude auth status >/dev/null 2>&1 || die "Claude Code is not logged in; run: claude auth login --claudeai"
echo "Claude account: authenticated"

MCP_VENV="${CLAUDE_JARVIS_MCP_VENV:-$ROOT/.venv}"
echo "Installing Python dependencies into $MCP_VENV"
python3 -m venv "$MCP_VENV"
"$MCP_VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$MCP_VENV/bin/python" -m pip install -r requirements.txt -r requirements-mcp.txt

DEFAULT_MCP_PATH="${JARVIS_TELEGRAM_MCP_PATH:-$HOME/codex-jarvis/telegram_actions_mcp.py}"
if [ -t 0 ]; then
    read -rp "CodexAsk telegram_actions_mcp.py [$DEFAULT_MCP_PATH]: " JARVIS_TELEGRAM_MCP_PATH
fi
JARVIS_TELEGRAM_MCP_PATH="${JARVIS_TELEGRAM_MCP_PATH:-$DEFAULT_MCP_PATH}"
[[ -f "$JARVIS_TELEGRAM_MCP_PATH" ]] || die "CodexAsk MCP implementation not found: $JARVIS_TELEGRAM_MCP_PATH"

sed \
    -e "s|__MCP_PYTHON__|$MCP_VENV/bin/python|g" \
    -e "s|__INSTALL_DIR__|$ROOT|g" \
    mcp_telegram_tools_config.json.example > mcp_telegram_tools_config.json
mkdir -p "$ROOT/runtime"
chmod 700 "$ROOT/runtime"

python3 -m py_compile claude_watcher.py cmd_queue.py mcp_telegram_tools.py remote_terminal.py

echo
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
J_OWNER_TG_ID=""
if [ -t 0 ]; then
    read -rp "Owner display name for the persona template (blank to skip): " J_OWNER_NAME || J_OWNER_NAME=""
    read -rp "Owner Telegram numeric id for the persona template (blank to skip): " J_OWNER_TG_ID || J_OWNER_TG_ID=""
    read -rp "Chat ids where profanity is suppressed, comma-separated (blank for none): " J_NOMATS || J_NOMATS=""
    read -rp "Shared persona directory [$ROOT/personas]: " J_PERSONA_DIR || J_PERSONA_DIR=""
    J_OWNER_NAME="$(_jarvis_clean "$J_OWNER_NAME")"
    J_OWNER_TG_ID="$(printf '%s' "$J_OWNER_TG_ID" | tr -cd '0-9')"
    J_NOMATS="$(printf '%s' "$J_NOMATS" | tr -cd '0-9,-')"
    J_PERSONA_DIR="$(_jarvis_clean "${J_PERSONA_DIR:-$ROOT/personas}")"
    if [ "$J_PERSONA_DIR" != "$ROOT/personas" ]; then
        mkdir -p "$J_PERSONA_DIR"
        [ -f "$J_PERSONA_DIR/default.md.example" ] || \
            cp "$ROOT/personas/default.md.example" "$J_PERSONA_DIR/default.md.example"
    fi
    ( umask 177; : > "$JARVIS_ENV" )
    {
        [ -n "$J_OWNER_NAME" ]  && printf 'JARVIS_OWNER_NAME=%s\n'  "$J_OWNER_NAME"
        [ -n "$J_OWNER_TG_ID" ] && printf 'JARVIS_OWNER_TG_ID=%s\n' "$J_OWNER_TG_ID"
        printf 'JARVIS_NOMATS_CHAT_IDS=%s\n' "$J_NOMATS"
        printf 'JARVIS_PERSONA_DIR=%s\n' "$J_PERSONA_DIR"
    } >> "$JARVIS_ENV"
    chmod 600 "$JARVIS_ENV"
    echo "Wrote $JARVIS_ENV (git-ignored; the watcher loads it via EnvironmentFile=)."
    echo "If the shared cmd_queue.py (from Claude-jarvis) runs from another directory,"
    echo "set the SAME JARVIS_PERSONA_DIR in its service unit too, otherwise"
    echo ".persona/.xpersona writes will not reach this watcher."
fi

# --- Relay authentication + systemd services --------------------------------
# The queue relay (cmd_queue.py) and the watcher (claude_watcher.py) are
# shared infrastructure: one instance of each serves every userbot instance
# that authenticates against them. A second run of this script (e.g. to add
# another instance_id) must not reinstall or restart either if they are
# already serving traffic.
QUEUE_SERVICE_NAME="${CLAUDE_JARVIS_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"
WATCHER_SERVICE_NAME="${CLAUDE_JARVIS_WATCHER_SERVICE:-jarvis-ask-watcher.service}"
RELAY_TOKENS_FILE="${JARVIS_RELAY_TOKENS_FILE:-/etc/jarvis-ask/relay-tokens.json}"
RELAY_OWNERS_FILE="${JARVIS_RELAY_OWNERS_FILE:-/etc/jarvis-ask/relay-owners.json}"
RELAY_TOKEN_ENV_FILE="${CLAUDE_JARVIS_RELAY_TOKEN_ENV:-/etc/jarvis-ask/relay-token.env}"

QUEUE_ACTIVE=0
WATCHER_ACTIVE=0
systemctl is-active --quiet "$QUEUE_SERVICE_NAME" 2>/dev/null && QUEUE_ACTIVE=1
systemctl is-active --quiet "$WATCHER_SERVICE_NAME" 2>/dev/null && WATCHER_ACTIVE=1

echo
echo "== Relay authentication =="
INSTANCE_ID=""
OWNER_TG_ID=""
RELAY_TOKEN=""
if [ -t 0 ]; then
    read -rp "Instance ID for this ClaudeAsk deployment (e.g. mybot; blank to configure later): " INSTANCE_ID || INSTANCE_ID=""
fi
if [[ -n "$INSTANCE_ID" ]]; then
    [[ "$INSTANCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || die "invalid instance id: $INSTANCE_ID"
    read -rp "Owner Telegram numeric ID for instance '$INSTANCE_ID'${J_OWNER_TG_ID:+ [$J_OWNER_TG_ID]}: " OWNER_TG_ID || OWNER_TG_ID=""
    OWNER_TG_ID="${OWNER_TG_ID:-$J_OWNER_TG_ID}"
    OWNER_TG_ID="$(printf '%s' "$OWNER_TG_ID" | tr -cd '0-9')"
    [[ -n "$OWNER_TG_ID" ]] || die "owner Telegram ID is required to authorize instance '$INSTANCE_ID'"

    sudo mkdir -p "$(dirname "$RELAY_OWNERS_FILE")"
    sudo python3 - "$RELAY_OWNERS_FILE" "$INSTANCE_ID" "$OWNER_TG_ID" <<'PY'
import json, os, sys
path, instance_id, owner_id = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
data[instance_id] = int(owner_id)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
    RELAY_TOKEN="$(sudo env JARVIS_RELAY_TOKENS_FILE="$RELAY_TOKENS_FILE" python3 "$ROOT/cmd_queue.py" --add-token "$INSTANCE_ID")"
    echo "Registered owner $OWNER_TG_ID for instance '$INSTANCE_ID' in $RELAY_OWNERS_FILE"

    if (( QUEUE_ACTIVE )); then
        # cmd_queue.py only reads the tokens/owners files at startup.
        sudo systemctl restart "$QUEUE_SERVICE_NAME"
        echo "Restarted $QUEUE_SERVICE_NAME to load the new token/owner mapping."
    fi

    if (( WATCHER_ACTIVE )); then
        echo "jarvis-ask-watcher is already active with a token for another instance;"
        echo "this setup only wires a single JARVIS_RELAY_TOKEN per watcher. To serve"
        echo "a second instance from the same watcher, combine tokens yourself into"
        echo "JARVIS_RELAY_TOKENS_JSON (see 'Network configuration' in the README)."
        echo "Your new token for '$INSTANCE_ID' (save it, it is not stored anywhere):"
        echo "  $RELAY_TOKEN"
    else
        sudo install -d -m 0755 "$(dirname "$RELAY_TOKEN_ENV_FILE")"
        printf 'JARVIS_RELAY_TOKEN=%s\n' "$RELAY_TOKEN" | sudo tee "$RELAY_TOKEN_ENV_FILE" >/dev/null
        sudo chmod 600 "$RELAY_TOKEN_ENV_FILE"
        echo "Wrote $RELAY_TOKEN_ENV_FILE (root-only; the watcher service reads it)."
    fi
else
    echo "No instance ID given; skipping relay token generation."
    echo "Re-run setup.sh once you're ready, or see 'Relay authentication and trust"
    echo "boundary' in the README to do it by hand."
fi

echo
echo "== systemd services =="
if (( QUEUE_ACTIVE )); then
    echo "$QUEUE_SERVICE_NAME is already active; reusing it (not reinstalling the relay)."
else
    QUEUE_UNIT_FILE="$(mktemp "/tmp/${QUEUE_SERVICE_NAME%.service}.XXXXXX")"
    sed \
        -e "s|__USER__|$(id -un)|g" \
        -e "s|__INSTALL_DIR__|$ROOT|g" \
        -e "s|__VENV_PYTHON__|$MCP_VENV/bin/python|g" \
        claude-jarvis-queue.service.example > "$QUEUE_UNIT_FILE"
    echo "Installing /etc/systemd/system/$QUEUE_SERVICE_NAME (sudo required)"
    sudo install -m 0644 "$QUEUE_UNIT_FILE" "/etc/systemd/system/$QUEUE_SERVICE_NAME"
    rm -f "$QUEUE_UNIT_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$QUEUE_SERVICE_NAME"
    QUEUE_ACTIVE=1
fi

if (( WATCHER_ACTIVE )); then
    echo "$WATCHER_SERVICE_NAME is already active; leaving it running."
else
    WATCHER_UNIT_FILE="$(mktemp "/tmp/${WATCHER_SERVICE_NAME%.service}.XXXXXX")"
    sed \
        -e "s|__USER__|$(id -un)|g" \
        -e "s|__INSTALL_DIR__|$ROOT|g" \
        -e "s|__VENV_PYTHON__|$MCP_VENV/bin/python|g" \
        -e "s|__JARVIS_TELEGRAM_MCP_PATH__|$JARVIS_TELEGRAM_MCP_PATH|g" \
        claude-jarvis-watcher.service.example > "$WATCHER_UNIT_FILE"
    echo "Installing /etc/systemd/system/$WATCHER_SERVICE_NAME (sudo required)"
    sudo install -m 0644 "$WATCHER_UNIT_FILE" "/etc/systemd/system/$WATCHER_SERVICE_NAME"
    rm -f "$WATCHER_UNIT_FILE"
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$WATCHER_SERVICE_NAME"
fi

echo
sudo systemctl --no-pager --full status "$QUEUE_SERVICE_NAME" "$WATCHER_SERVICE_NAME" || true
echo
if [[ -n "$INSTANCE_ID" && -n "$RELAY_TOKEN" ]]; then
    echo "Backend ready. Upload claude_ask.py to the Telethon userbot, reply .lm, then run:"
    echo "  .asknet token $RELAY_TOKEN"
    echo "  .asknet local $INSTANCE_ID"
else
    echo "Backend ready. Upload claude_ask.py to the Telethon userbot and reply .lm,"
    echo "then configure it with .asknet once you have a relay token (see README)."
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
