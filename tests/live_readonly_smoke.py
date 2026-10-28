"""Read-only MCP verification against the user's existing Maya GUI session."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT / "evidence"


async def main():
    results = {}
    params = StdioServerParameters(command=sys.executable, args=[str(PROJECT / "scripts" / "run_server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            results["mcp_server"] = init.serverInfo.model_dump()
            tool_list = await session.list_tools()
            results["tools"] = [t.name for t in tool_list.tools]
            assert len(results["tools"]) == 13

            async def invoke(name, arguments=None):
                response = await session.call_tool(name, arguments or {})
                if response.isError:
                    raise RuntimeError(str(response.content))
                if name == "capture_frame":
                    assert any(c.type == "image" for c in response.content)
                    return {"image_returned_via_mcp": True}
                return json.loads("\n".join(c.text for c in response.content if c.type == "text"))

            results["status"] = await invoke("get_status")
            before = await invoke("get_scene_info")
            results["scene_before"] = before
            controls, offset = [], 0
            while True:
                page = await invoke("discover_rig", {"limit": 500, "offset": offset})
                controls.extend(page["controls"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            results["rig_summary"] = {k: v for k, v in page.items() if k != "controls"}
            results["control_count"] = len(controls)
            inspected = await invoke("inspect_nodes", {"nodes": controls[:12]})
            (EVIDENCE / "live_rig_inventory.json").write_text(json.dumps({"controls": controls, "sample_inspection": inspected}, ensure_ascii=False, indent=2), encoding="utf-8")
            nodes = before["selection"] or controls[:1]
            results["pose"] = await invoke("get_pose", {"nodes": nodes})
            results["curve_query"] = await invoke("get_animation_curves", {"nodes": nodes})
            image_path = PROJECT / "output" / "live_maya_viewport.png"
            results["capture"] = await invoke("capture_frame", {"path": str(image_path), "width": 960, "height": 540})
            results["capture"]["path"] = str(image_path)
            results["capture"]["sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
            after = await invoke("get_scene_info")
            for field in ("scene", "modified", "time", "selection", "animation_layers"):
                assert before[field] == after[field], (field, before[field], after[field])
            after_pose = await invoke("get_pose", {"nodes": nodes})
            assert results["pose"] == after_pose
            results["scene_state_preserved"] = True
    results["verified"] = ["MCP_initialize", "MCP_tools_list", "native_Maya_GUI_status", "controller_paging", "channel_inspection", "evaluated_world_pose", "curve_query", "MCP_image_capture", "scene_state_preserved"]
    report = EVIDENCE / "live_mcp_report.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verified": results["verified"], "control_count": len(controls), "joint_count": page["joint_count"], "maya": results["status"]["maya_version"], "report": str(report)}, ensure_ascii=False))


asyncio.run(main())
