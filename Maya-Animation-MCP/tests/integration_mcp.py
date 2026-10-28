"""Real MCP stdio client -> external server -> native Maya -> scene assertions."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]
MODE = sys.argv[1] if len(sys.argv) > 1 else "native"
OUTPUT = PROJECT / "evidence" / MODE / os.environ.get("MAYA_MCP_TEST_RUN", "")
CONNECTION = OUTPUT / "connection.json"
CHECKS = []


def check(name, condition, detail=None):
    assert condition, (name, detail)
    CHECKS.append({"check": name, "pass": True, "detail": detail})


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(PROJECT / "scripts" / "run_server.py")], env={"MAYA_MCP_CONNECTION": str(CONNECTION)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            check("MCP initialize", initialized.serverInfo.name == "Maya Animation MCP")
            tools = await session.list_tools()
            check("MCP tool discovery", len(tools.tools) == 13, [t.name for t in tools.tools])

            async def invoke(name, arguments=None, error=False):
                result = await session.call_tool(name, arguments or {})
                text = "\n".join(c.text for c in result.content if c.type == "text")
                check(name + (" expected rejection" if error else " success"), bool(result.isError) == error, text if error or result.isError else None)
                if error:
                    return text
                if name == "capture_frame":
                    check("MCP image content", any(c.type == "image" for c in result.content))
                    return None
                return json.loads(text)

            status = await invoke("get_status")
            check("native Maya version", status["maya_version"].startswith("2022"), status)
            initial = await invoke("get_scene_info")
            rig = await invoke("discover_rig")
            ctrl = "|MCP_TEST_CTRL"
            check("NURBS control discovery", ctrl in rig["controls"])
            inspected = await invoke("inspect_nodes", {"nodes": [ctrl]})
            attrs = inspected["nodes"][0]["attributes"]
            check("custom IK/FK enum inspection", any(a["name"] == "ikFk" and "FK:IK" in a["enum"] for a in attrs))
            await invoke("create_animation_layer", {"name": "MCP_Test_Action", "nodes": [ctrl], "attributes": ["translateX"], "override": True})
            keys = [{"node": ctrl, "attribute": "translateX", "time": t, "value": v} for t, v in [(1, 0), (12, 3), (24, 0)]]
            await invoke("set_keyframes", {"keys": keys, "layer": "MCP_Test_Action", "in_tangent": "linear", "out_tangent": "linear"})
            pose = await invoke("get_pose", {"nodes": [ctrl, "|MCP_TEST_JOINT"], "frame": 12})
            check("evaluated control key", abs(pose["nodes"][0]["attributes"]["translateX"] - 3) < 1e-6, pose)
            if "world_matrix" in pose["nodes"][1]:
                check("rig constraint evaluates", abs(pose["nodes"][1]["world_matrix"][12] - 3) < 1e-6)
            scene = await invoke("get_scene_info")
            check("pose query restores time", scene["time"] == initial["time"])
            curves = await invoke("get_animation_curves", {"nodes": [ctrl]})
            check("animation curve keys", any(len(c["keys"]) == 3 for c in curves["curves"]), curves)
            bad = keys + [{"node": ctrl, "attribute": "scaleX", "time": 12, "value": 2}]
            await invoke("set_keyframes", {"keys": bad, "layer": "MCP_Test_Action"}, error=True)
            await invoke("create_animation_layer", {"name": "MCP_Test_Action", "nodes": [ctrl], "attributes": ["translateX"]}, error=True)
            await invoke("execute_python", {"code": "print('should not execute')"}, error=True)
            await invoke("save_scene_copy", {"path": str(PROJECT.parent / "out_of_root.ma")}, error=True)
            scene_path = OUTPUT / "mcp_animation_test.ma"
            await invoke("save_scene_copy", {"path": str(scene_path)})
            check("scene copy exists", scene_path.is_file() and scene_path.stat().st_size > 1000)
            after = await invoke("get_scene_info")
            check("copy preserves original scene identity and dirty state", after["scene"] == scene["scene"] and after["modified"] == scene["modified"], after)
            await invoke("save_scene_copy", {"path": str(scene_path)}, error=True)
            if MODE == "gui":
                await invoke("capture_frame", {"path": str(OUTPUT / "frame12.png"), "frame": 12, "width": 960, "height": 540})
                seq = OUTPUT / "sequence"
                seq.mkdir(exist_ok=True)
                pb = await invoke("playblast", {"path": str(seq / "test"), "start": 1, "end": 24, "width": 640, "height": 360})
                check("native viewport sequence", len(pb["files"]) == 24, {"count": len(pb["files"])})

    info = json.loads(CONNECTION.read_text())
    with socket.create_connection((info["host"], info["port"]), timeout=5) as sock:
        request = {"id": "bad-auth", "token": "invalid", "method": "get_status", "params": {}}
        sock.sendall(json.dumps(request).encode() + b"\n")
        response = json.loads(sock.makefile("rb").readline())
    check("reject unauthenticated bridge requests", response["error"]["code"] == "UNAUTHORIZED")
    report = {"mode": MODE, "scheduler": "test-only main-thread queue" if MODE == "native" else "Maya executeDeferred", "checks": CHECKS, "passed": len(CHECKS)}
    (OUTPUT / "mcp_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": MODE, "passed": len(CHECKS), "report": str(OUTPUT / "mcp_report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
