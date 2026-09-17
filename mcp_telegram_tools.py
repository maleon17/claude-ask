#!/usr/bin/env python3
"""Compatibility launcher for the shared Codex/Claude Telegram MCP server.

The implementation is versioned with the standalone Codex product. Keeping
this historical path means the existing Claude userbot configuration keeps
working while both assistants use one trigger/action schema and queue.
"""

import os
from pathlib import Path
from runpy import run_path

path = os.environ.get("JARVIS_TELEGRAM_MCP_PATH")
if not path:
    raise RuntimeError(
        "JARVIS_TELEGRAM_MCP_PATH is required; point it at the CodexAsk "
        "telegram_actions_mcp.py checkout."
    )
target = Path(path).expanduser()
if not target.is_file():
    raise RuntimeError(f"JARVIS_TELEGRAM_MCP_PATH does not exist: {target}")
run_path(str(target), run_name="__main__")
