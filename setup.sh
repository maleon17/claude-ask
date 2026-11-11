#!/usr/bin/env bash
# Installer for the ClaudeAsk backend: the Claude Code worker
# (claude_watcher.py), plus the shared pieces it needs from telegram-ai-core
# (queue relay, Telegram-action MCP server, userbot module) -- that repository
# is found next to this one or cloned automatically.
#
# Safe to re-run: existing jarvis.env keys are kept, running services are left
# running, and a token added for another instance takes effect without a relay
# restart.
#
# Every question can be answered through the environment instead, which makes
# an unattended install possible:
#   TELEGRAM_AI_CORE_DIR      an existing telegram-ai-core checkout
#   JARVIS_OWNER_NAME, JARVIS_OWNER_TG_ID, JARVIS_NOMATS_CHAT_IDS, JARVIS_PERSONA_DIR
#   JARVIS_INSTANCE_ID        instance to register with the relay
#   TELEGRAM_AI_CONFIG_DIR    credentials and registry (default ~/.config/telegram-ai)
#   JARVIS_STATE_DIR          queues and uploads       (default ~/.local/state/telegram-ai)
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
# The core checkout recorded in the component registry, if any.
registered_core() {
    python3 - <<'PY' 2>/dev/null || true
import json, os
base = os.environ.get("TELEGRAM_AI_CONFIG_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "telegram-ai")
with open(os.path.join(base, "registry.json"), encoding="utf-8") as handle:
    print(json.load(handle)["components"]["core"]["path"])
PY
}

echo "== Claude Jarvis setup =="
need python3
need systemctl
[[ $EUID -eq 0 ]] || need sudo

CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
[[ -n "$CLAUDE_BIN" ]] || die "Claude Code CLI not found; install it: curl -fsSL https://claude.ai/install.sh | bash"
"$CLAUDE_BIN" auth status >/dev/null 2>&1 || die "Claude Code is not logged in; run: $CLAUDE_BIN auth login --claudeai"
echo "Claude account: authenticated ($CLAUDE_BIN)"

# --- telegram-ai-core: relay, Telegram-action MCP server, userbot module -----
CORE_REPO_URL="${TELEGRAM_AI_CORE_REPO:-https://github.com/maleon17/telegram-ai-core.git}"
CORE_DIR=""
for candidate in "${TELEGRAM_AI_CORE_DIR:-}" "$(registered_core)" "$ROOT/../telegram-ai-core" "$HOME/telegram-ai-core"; do
    if [[ -n "$candidate" && -f "$candidate/install_relay.sh" && -f "$candidate/telegram_actions_mcp.py" ]]; then
        CORE_DIR="$(cd "$candidate" && pwd)"
        break
    fi
done
if [[ -z "$CORE_DIR" ]]; then
    need git
    CORE_DIR="$(dirname "$ROOT")/telegram-ai-core"
    echo "Fetching telegram-ai-core into $CORE_DIR"
    git clone --quiet --depth 1 "$CORE_REPO_URL" "$CORE_DIR" \
        || die "could not clone $CORE_REPO_URL (set TELEGRAM_AI_CORE_DIR to an existing checkout)"
fi
echo "telegram-ai-core: $CORE_DIR"

# Installs the relay's virtualenv and service if they are not there yet; a
# relay that is already running is left alone.
"$CORE_DIR/install_relay.sh"
CORE_PYTHON="$CORE_DIR/.venv/bin/python"
[[ -x "$CORE_PYTHON" ]] || die "telegram-ai-core has no virtualenv after install_relay.sh"
core_registry() { "$CORE_PYTHON" "$CORE_DIR/registry.py" "$@"; }

cat > mcp_telegram_tools_config.json <<JSON
{
  "mcpServers": {
    "telegram_actions": {
      "command": "$CORE_PYTHON",
      "args": ["$CORE_DIR/telegram_actions_mcp.py"]
    }
  }
}
JSON
mkdir -p "$ROOT/runtime"
chmod 700 "$ROOT/runtime"

python3 -m py_compile claude_watcher.py claude_ask.py remote_terminal.py
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
echo "Wrote $JARVIS_ENV (git-ignored; the worker loads it via EnvironmentFile=)."

# --- Relay authentication + worker service -----------------------------------
# The relay and this worker are shared infrastructure: one of each serves
# every userbot instance that authenticates against them. A second run of this
# script (e.g. to add another instance_id) must not restart either while they
# are serving traffic.
QUEUE_SERVICE_NAME="${JARVIS_RELAY_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"
WATCHER_SERVICE_NAME="${CLAUDE_JARVIS_WATCHER_SERVICE:-jarvis-ask-watcher.service}"
WATCHER_ACTIVE=0
systemctl is-active --quiet "$WATCHER_SERVICE_NAME" 2>/dev/null && WATCHER_ACTIVE=1

# The relay (installed above, possibly long ago) is the source of truth for
# where credentials and queues live.
RELAY_CONFIG_DIR="$(core_registry get relay config_dir)"
STATE_DIR="$(core_registry get relay state_dir)"
[[ -n "$RELAY_CONFIG_DIR" && -n "$STATE_DIR" ]] || die "the relay is not registered; run $CORE_DIR/install_relay.sh"
WORKER_TOKEN_ENV="$RELAY_CONFIG_DIR/relay-token-claude.env"

# Lets the relay serve .persona from this checkout and the neighbours find it.
core_registry register claude-ask "path=$ROOT" "unit=$WATCHER_SERVICE_NAME" \
    "persona_dir=$J_PERSONA_DIR" "owner_name=$J_OWNER_NAME" "owner_tg_id=$J_OWNER_TG_ID"

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

    RELAY_TOKEN="$("$CORE_DIR/register_instance.sh" "$INSTANCE_ID" "$OWNER_TG_ID" "$WORKER_TOKEN_ENV")"
    # The first instance runs on this host's own Claude login. Any later one
    # is somebody else's userbot: it gets an isolated Claude config directory
    # and its own login, so it never spends the first owner's subscription.
    PRIMARY_INSTANCE="$(env_file_get "$JARVIS_ENV" CLAUDE_JARVIS_PRIMARY_INSTANCE)"
    if [[ -z "$PRIMARY_INSTANCE" ]]; then
        env_file_set "$JARVIS_ENV" CLAUDE_JARVIS_PRIMARY_INSTANCE "$INSTANCE_ID"
    elif [[ "$PRIMARY_INSTANCE" != "$INSTANCE_ID" ]]; then
        echo "NOTE: '$INSTANCE_ID' is an additional instance (the primary one is '$PRIMARY_INSTANCE')."
        echo "It uses its own Claude account; log it in once:"
        echo "  CLAUDE_CONFIG_DIR=$ROOT/runtime/accounts/$INSTANCE_ID $CLAUDE_BIN auth login --claudeai"
    fi
    echo "Registered owner $OWNER_TG_ID and a new token for instance '$INSTANCE_ID' in $RELAY_CONFIG_DIR"
    echo "(a running relay picks this up by itself; no restart needed)."
    if (( WATCHER_ACTIVE )); then
        echo "NOTE: $WATCHER_SERVICE_NAME is already running and reads $WORKER_TOKEN_ENV"
        echo "only at start. Restart it once no .ask is in flight:"
        echo "  sudo systemctl restart $WATCHER_SERVICE_NAME"
    fi
else
    echo "No instance ID given; skipping relay token generation."
    echo "Re-run setup.sh once you're ready, or see 'Relay authentication' in the"
    echo "telegram-ai-core README to do it by hand."
fi

echo
echo "== systemd service =="
if (( WATCHER_ACTIVE )); then
    echo "$WATCHER_SERVICE_NAME is already active; leaving it running."
else
    UNIT_FILE="$(mktemp)"
    sed \
        -e "s|__USER__|$(id -un)|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__INSTALL_DIR__|$ROOT|g" \
        -e "s|__PYTHON__|$CORE_PYTHON|g" \
        -e "s|__CLAUDE_BIN__|$CLAUDE_BIN|g" \
        -e "s|__CONFIG_DIR__|$RELAY_CONFIG_DIR|g" \
        -e "s|__STATE_DIR__|$STATE_DIR|g" \
        claude-jarvis-watcher.service.example > "$UNIT_FILE"
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
    echo "  .asknet check"
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

bash "$CORE_DIR/scripts/setup_userbot_network.sh" claude_ask.py
