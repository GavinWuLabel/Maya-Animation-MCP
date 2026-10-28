"""Generate local launch/config snippets after extraction; does not edit Codex settings."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def entry(background=False):
    lines = ['[mcp_servers.maya_animation]', 'command = '+json.dumps(str(ROOT/'.venv'/'Scripts'/'python.exe')), 'args = ['+json.dumps(str(ROOT/'scripts'/'run_server.py'))+']', 'startup_timeout_sec = 20', 'tool_timeout_sec = 45']
    if background:
        lines += ['env = { MAYA_MCP_CONNECTION = '+json.dumps(str(ROOT/'runtime'/'background_connection.json'))+' }']
    return '\n'.join(lines)+'\n'
def main():
    (ROOT/'codex-mcp.toml').write_text(entry(),encoding='utf8')
    (ROOT/'codex-mcp-background.toml').write_text(entry(True),encoding='utf8')
    command='import runpy; runpy.run_path('+json.dumps((ROOT/'maya_side'/'start_in_maya.py').as_posix())+')'
    (ROOT/'START_MAYA_BRIDGE.mel').write_text('// Generated for this extracted folder. Run configure.py again after moving.\npython('+json.dumps(command)+');\n',encoding='utf8')
    print('Generated GUI/background MCP configuration snippets and START_MAYA_BRIDGE.mel. Existing application settings are unchanged.')
if __name__=='__main__':main()
