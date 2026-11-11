# ClaudeAsk

> Part of **[Telegram AI](https://github.com/maleon17/telegram-ai)** — Claude/Codex from dedicated bots or directly inside your Telegram account.

**Invoke Claude from an ordinary Telegram conversation, as your own account.**

ClaudeAsk combines a Telethon/Hikka userbot with a Claude Code worker and resumable per-chat sessions. Type `.ask` in a chat, the backend does the work, and the loaded module can use your live Telegram session for real actions such as reading history, searching chats, sending files or managing supported conversation workflows.

This is deliberately different from **[claude-telegram-bridge](https://github.com/maleon17/claude-telegram-bridge)**: there is no separate bot conversation acting as the UI. ClaudeAsk lives inside the conversations you already use.

## How it works

```mermaid
flowchart LR
    C["Telegram chat<br/>.ask"] --> U["ClaudeAsk<br/>Telethon module"]
    U --> R["Authenticated<br/>queue relay"]
    R --> W["Claude Code worker<br/>resumable sessions"]
    W --> R --> U
    W --> M["Telegram actions MCP"]
    M --> R --> U
    U <--> T["Live Telethon session"]
```

The important trust boundary is intentional: **the backend does not own your Telegram account**. Telegram actions are queued back to the module that holds the live Telethon session. The action gets a real result before the agent can treat it as completed.

The module submits `.ask` work to the relay's `/ask` endpoint. Queue items, progress, answers and uploaded files are exchanged through a private state directory (`~/.local/state/telegram-ai` by default, mode `0700`) — never `/tmp`, because they carry chat text and media.

## What is included

- `claude_ask.py` — loadable ClaudeAsk userbot module
- `claude_watcher.py` — Claude Code backend with resumable sessions
- `mcp_telegram_tools.py` — launcher for the Telegram-action MCP implementation from the CodexAsk checkout
- `cmd_queue.py` — authenticated shared HTTP queue relay
- `setup.sh` and service examples — backend installation

## Commands

ClaudeAsk provides:

- `.ask` — ask Claude
- `.search` — search workflow
- `.translate` — translation workflow
- `.new` — reset/start a fresh Claude context

Network configuration is stored per userbot instance through `.asknet`.

Trigger rules live in the shared `ClaudeAsk` namespace, enabling coordination when both products and the optional `jarvis_ask.py` coordinator are loaded.

## Setup

Requirements:

- Linux with Python 3.10+
- Claude Code CLI authenticated with `claude auth login --claudeai`
- `systemd` and `sudo` (only to install the two service units)
- the [CodexAsk](https://github.com/maleon17/codex-ask) checkout next to this one — ClaudeAsk uses the Telegram-action MCP server that ships there (you do not have to install or run CodexAsk itself)
- a supported Telethon/Hikka userbot; this project targets [Heroku](https://github.com/coddrago/Heroku)

Heroku may run on the same host or in Docker on a separate host; the latter is a deployment choice, not a requirement.

```bash
git clone https://github.com/maleon17/codex-ask.git
git clone https://github.com/maleon17/claude-ask.git
cd claude-ask
./setup.sh
```

`setup.sh` does everything on the backend side in one pass:

- finds the CodexAsk checkout's `telegram_actions_mcp.py` (a sibling `codex-ask` directory is detected automatically; otherwise it asks, or reads `JARVIS_TELEGRAM_MCP_PATH`) and stops right there with instructions if it is missing
- creates the MCP virtual environment and `mcp_telegram_tools_config.json`
- writes the persona env (`jarvis.env`), keeping any other keys already in that file
- registers an owner and generates a relay token for an `instance_id` you choose (1–64 letters/digits/`_`/`-`, starting with a letter or digit), under [Relay authentication](#relay-authentication-and-trust-boundary) — printing the exact `.asknet` commands to paste into Telegram at the end
- installs and starts the `jarvis-ask-cmd-queue` and `jarvis-ask-watcher` systemd services (skipping either one that's already running, since both are shared infrastructure — safe to re-run for a second instance)
- optionally builds the graphify code map
- optionally builds and starts a local Docker userbot container (Heroku image, first-run Telegram sign-in included), or configures Tailscale on this host for a remote userbot

Every question can also be answered through environment variables (listed at the top of `setup.sh`), so the install can run unattended.

The only things it cannot do for you: the Claude Code login itself, and anything that has to happen inside Telegram (loading the module, running `.asknet`, the userbot's own first-time Telegram sign-in if you're not using the Docker option).

## Load the userbot module

Send `claude_ask.py` as a document to the userbot's test channel and reply with:

```text
.lm
```

Then run `.asknet` with the exact `<token>` and `<instance_id>` `setup.sh` printed to your terminal a moment ago (or that `cmd_queue.py --add-token <instance_id>` printed, if you generated them by hand — see [Relay authentication](#relay-authentication-and-trust-boundary)). That token is shown once and stored nowhere else in plaintext; if you lost it, re-run `setup.sh` or `cmd_queue.py --add-token` again to mint another one for the same instance (older tokens stay valid until revoked):

```text
.asknet token <token>
.asknet local <instance_id>
```

or, for a remote backend:

```text
.asknet tailnet <instance_id> <backend_url>
```

Send the token command in a private chat, not a group. A single userbot can share the same `instance_id`/token between ClaudeAsk and CodexAsk; separate userbots each need their own.

### Remote userbot over Tailscale

The **remote userbot container** must also have `tailscale` and `tailscaled` installed and join the same tailnet. Install the package in its Docker image so container recreation does not remove the binaries. For an Ubuntu/Debian-based image, add a build step like this before starting Heroku:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://tailscale.com/install.sh -o /tmp/tailscale-install.sh \
    && sh /tmp/tailscale-install.sh \
    && rm /tmp/tailscale-install.sh
```

The Ask module starts `tailscaled` in [userspace networking mode](https://tailscale.com/docs/concepts/userspace-networking) on module load, with an HTTP proxy at `localhost:1056`. It requires a persistent `/data` mount for `/data/tailscale/tailscaled.state`. Set `.asknet tailnet <instance_id> http://<backend-tailnet-ip>:9092` and `.asknet token <token>`, then reload the module so `client_ready` starts the local Tailscale proxy. Run the first interactive login inside that container:

```bash
docker exec -it <userbot-container> tailscale --socket=/var/run/tailscale/tailscaled.sock up
```

Open the URL Tailscale prints and approve the device. The module can restart a previously authenticated daemon, but it cannot install Tailscale or perform the first login for you.

The saved network settings survive later module updates.

Verify `.ask`, `.new`, and at least one request that uses a real Telegram tool such as `list_triggers`, `read_history`, or `search_chat`.

## Relay authentication and trust boundary

The relay keeps two small maps in `~/.config/telegram-ai/` (override with `JARVIS_RELAY_CONFIG_DIR`, or name the files directly with `JARVIS_RELAY_TOKENS_FILE` / `JARVIS_RELAY_OWNERS_FILE`):

- `relay-tokens.json` — `sha256(token)` → `instance_id`; the token itself is never stored there
- `relay-owners.json` — `instance_id` → Telegram owner ID

Both are mode `0600` and owned by the user the relay runs as. No `sudo` is involved in managing them, and the running relay notices an edit by itself — adding an instance does not restart it or drop requests in flight.

`setup.sh` registers the owner and mints the token, then records that token in `relay-token-claude.env` in the same directory. The watcher's systemd unit reads it through `EnvironmentFile=` as `JARVIS_RELAY_TOKENS_JSON` (an `instance_id` → token map), which is how its local Telegram MCP client authenticates back to the relay. CodexAsk keeps its own `relay-token-codex.env`, so installing one product never overwrites the other's credentials. The raw token is printed to your terminal once — that is the `<token>` for `.asknet token` in [Load the userbot module](#load-the-userbot-module).

To add another instance later, re-run `setup.sh`: it reuses the running relay and watcher, and only asks you to restart the watcher (which reads its token file at start) once nothing is in flight.

Managing instances by hand, with the virtualenv's interpreter:

```bash
.venv/bin/python cmd_queue.py --list                      # instances, owners, token counts
.venv/bin/python cmd_queue.py --set-owner mybot 123456789
.venv/bin/python cmd_queue.py --add-token mybot           # prints the new token once
.venv/bin/python cmd_queue.py --revoke-tokens mybot       # invalidates every token of that instance
```

The authenticated userbot reports `requester_id`. The relay cannot independently prove the original Telegram sender, so the authenticated userbot remains an explicit trust boundary. Owner authorization is nevertheless derived server-side from the token's instance mapping; the client cannot simply claim to be the owner.

Requests, tools, results, personas, resets and registered download artifacts are scoped by instance.

## Network configuration

`JARVIS_RELAY_BIND` controls relay listen addresses and defaults safely to `127.0.0.1`.

`JARVIS_STATE_DIR` (default `~/.local/state/telegram-ai`) holds the queues, uploads and request manifests. The relay and every worker on the host must use the same value — they exchange files there.

`CLAUDE_JARVIS_BACKEND_URL` configures the queue relay's network address for the module.

Voice transcription reads its Mistral key first from `/data/mistral_api_key.txt`, then falls back to `MISTRAL_API_KEY` in the environment. Keep either source outside the repository.

## Manual setup (without setup.sh)

Everything `setup.sh` automates, as raw commands, for anyone who wants full control or is scripting a different deployment.

Python dependencies and MCP config:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -r requirements-mcp.txt
sed -e "s|__MCP_PYTHON__|$PWD/.venv/bin/python|g" -e "s|__INSTALL_DIR__|$PWD|g" \
    mcp_telegram_tools_config.json.example > mcp_telegram_tools_config.json
mkdir -p runtime && chmod 700 runtime
```

Relay owner and token for an instance named `mybot`, run as the user the services will run as (no `sudo`):

```bash
.venv/bin/python cmd_queue.py --set-owner mybot 123456789   # your numeric Telegram ID
.venv/bin/python cmd_queue.py --add-token mybot \
    --worker-env ~/.config/telegram-ai/relay-token-claude.env
```

The token is printed once; save it for `.asknet token`. `--worker-env` also records it for the watcher.

systemd services, after replacing the placeholders in the two `.service.example` files — `__USER__`, `__HOME__`, `__INSTALL_DIR__`, `__VENV_PYTHON__` (`$PWD/.venv/bin/python`), `__CLAUDE_BIN__` (`command -v claude`), `__RELAY_CONFIG_DIR__` (`~/.config/telegram-ai`), `__STATE_DIR__` (`~/.local/state/telegram-ai`) and `__JARVIS_TELEGRAM_MCP_PATH__` (CodexAsk's `telegram_actions_mcp.py`):

```bash
sudo install -m 0644 claude-jarvis-queue.service.example /etc/systemd/system/jarvis-ask-cmd-queue.service
sudo install -m 0644 claude-jarvis-watcher.service.example /etc/systemd/system/jarvis-ask-watcher.service
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis-ask-cmd-queue.service jarvis-ask-watcher.service
```

Skip the first `install`/`enable` if `jarvis-ask-cmd-queue.service` is already running for this deployment — it's shared infrastructure; do not run two copies. The relay refuses to start until at least one instance is registered.

Local Docker userbot, without the `scripts/setup_userbot_network.sh` wrapper: copy `deploy/userbot/{Dockerfile,entrypoint.sh,compose.yaml}` into a project directory, set `USERBOT_TG_ID` in a `.env` file next to them, put `claude_ask.py` under `modules/ClaudeAsk.py`, then `docker compose up -d --build` and attach once (`docker attach --sig-proxy=false --detach-keys=ctrl-p,ctrl-q <container>`) to complete Heroku's Telegram sign-in. For a remote userbot, see [Remote userbot over Tailscale](#remote-userbot-over-tailscale) above — those commands don't depend on any script either.

Then continue from [Load the userbot module](#load-the-userbot-module).

## Updating

Reinstall the current module from `main` with:

```text
.dlm https://raw.githubusercontent.com/maleon17/claude-ask/main/claude_ask.py
```

A plain `docker cp` does not reload an already-running module. `.asknet` configuration persists in the userbot database.

## Verification

```bash
./scripts/run_tests.sh
systemctl status jarvis-ask-watcher
journalctl -u jarvis-ask-watcher -f
```

`requirements-dev.txt` declares pytest. CI runs the test suite without requiring secrets or live Telegram services.

## ClaudeAsk + CodexAsk

ClaudeAsk and **[CodexAsk](https://github.com/maleon17/codex-ask)** are independent agent backends with separate histories, but intentionally share selected infrastructure and trigger coordination.

When both modules and the optional `jarvis_ask.py` coordinator are loaded, it chooses one trigger action per incoming message. Without it, the modules retain their separate legacy trigger handling.

For the wider picture, see **[Telegram AI](https://github.com/maleon17/telegram-ai)**.

## License

MIT, Copyright (c) 2026 maleon17.
