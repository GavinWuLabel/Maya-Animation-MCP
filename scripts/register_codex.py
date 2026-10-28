"""Add only this named MCP entry; preserve other Codex configuration verbatim."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
NAME = "maya_animation"
HEADER = f"[mcp_servers.{NAME}]"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SCRIPT = ROOT / "scripts" / "run_server.py"
ENTRY = f'''{HEADER}
command = {json.dumps(str(PYTHON))}
args = [{json.dumps(str(SCRIPT))}]
startup_timeout_sec = 20
tool_timeout_sec = 45
'''

if not PYTHON.is_file():
    raise SystemExit("Run scripts/setup.ps1 first")
existing = CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else ""
# Avoid guessing how to merge an existing customized entry, including child tables.
pattern = r'^\[mcp_servers\.(?:maya_animation|"maya_animation")(?:\]|\.)'
if re.search(pattern, existing, re.MULTILINE):
    raise SystemExit("maya_animation is already configured; no changes made")
candidate = existing.rstrip() + "\n\n" + ENTRY
if sys.version_info >= (3, 11):
    import tomllib
    parsed = tomllib.loads(candidate)
    assert parsed["mcp_servers"][NAME]["command"] == str(PYTHON)
backup = CONFIG.with_name("config.toml.before-maya-animation-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
CONFIG.parent.mkdir(parents=True, exist_ok=True)
if CONFIG.is_file():
    shutil.copy2(CONFIG, backup)
else:
    backup = None
CONFIG.write_text(candidate, encoding="utf-8")
(ROOT / "codex-mcp.toml").write_text(ENTRY, encoding="utf-8")
print(json.dumps({"configured": NAME, "config": str(CONFIG), "backup": str(backup) if backup else None}, ensure_ascii=False))
