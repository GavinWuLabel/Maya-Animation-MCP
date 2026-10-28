"""MCP stdio runs outside Maya; Maya uses only its bundled standard library."""
import asyncio
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from .bridge_client import BridgeError, call

mcp = FastMCP("Maya Animation MCP", instructions=(
    "Inspect get_status, get_scene_info, discover_rig and inspect_nodes before writing animation. "
    "Curve transforms are candidates, not a verified rig map. Use exact full DAG paths. "
    "Work on writable controls, not driven deform joints. Read enum attributes before IK/FK or space switches. "
    "Save a new scene copy before production edits; inspect layers before keying. "
    "No automatic retries after timeouts. Inspect actual evaluated poses and frame images. "
    "Generic IK/FK matching and motion reconstruction require a rig-specific mapping."
))


async def invoke(method: str, params: dict | None = None):
    try:
        return await asyncio.to_thread(call, method, params)
    except BridgeError as exc:
        raise ToolError(str(exc)) from exc


class Keyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str = Field(min_length=1)
    attribute: str = Field(min_length=1)
    time: FiniteFloat
    value: FiniteFloat


Tangent = Literal["auto", "linear", "flat", "spline", "clamped", "plateau"]
OutTangent = Literal["auto", "linear", "flat", "spline", "clamped", "plateau", "step", "stepnext"]


@mcp.tool()
async def get_status() -> dict:
    """Read Maya/Python/bridge versions, current scene, batch state and capabilities."""
    return await invoke("get_status")


@mcp.tool()
async def get_scene_info() -> dict:
    """Read scene path/dirty state, time unit, range, namespaces, references and layers."""
    return await invoke("get_scene_info")


@mcp.tool()
async def discover_rig(root: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Page through NURBS control candidates under a root; candidates need inspection."""
    if not 1 <= limit <= 500 or offset < 0:
        raise ToolError("limit must be 1..500 and offset nonnegative")
    return await invoke("discover_rig", {"root": root, "limit": limit, "offset": offset})


@mcp.tool()
async def inspect_nodes(nodes: list[str]) -> dict:
    """Read exact node channels, locks, settable state, custom enum values and constraints."""
    return await invoke("inspect_nodes", {"nodes": nodes})


@mcp.tool()
async def get_pose(nodes: list[str], frame: float | None = None) -> dict:
    """Read local channels and world matrices at a frame, restoring the current time."""
    return await invoke("get_pose", {"nodes": nodes, "frame": frame})


@mcp.tool()
async def create_animation_layer(name: str, nodes: list[str], attributes: list[str], override: bool = False) -> dict:
    """Create a new layer for explicit writable control channels; never replace a layer."""
    return await invoke("create_animation_layer", {"name": name, "nodes": nodes, "attributes": attributes, "override": override})


@mcp.tool()
async def set_keyframes(keys: list[Keyframe], layer: str | None = None, in_tangent: Tangent = "auto", out_tangent: OutTangent = "auto") -> dict:
    """Key final evaluated values in an undo transaction. Use step output for enum/bool switches."""
    if not 1 <= len(keys) <= 10000:
        raise ToolError("Provide 1..10000 keys per call")
    return await invoke("set_keyframes", {"keys": [k.model_dump() for k in keys], "layer": layer, "in_tangent": in_tangent, "out_tangent": out_tangent})


@mcp.tool()
async def get_animation_curves(nodes: list[str], start: float | None = None, end: float | None = None) -> dict:
    """Read animation curves and key times/values for the given controls."""
    return await invoke("get_animation_curves", {"nodes": nodes, "start": start, "end": end})


@mcp.tool()
async def set_current_time(frame: float) -> dict:
    """Evaluate Maya at the requested frame."""
    return await invoke("set_current_time", {"frame": frame})


@mcp.tool()
async def save_scene_copy(path: str) -> dict:
    """Save a new .ma under the bridge output root. Existing paths are rejected."""
    return await invoke("save_scene_copy", {"path": path})


@mcp.tool()
async def playblast(path: str, start: float, end: float, width: int = 960, height: int = 540) -> dict:
    """Create a PNG playblast sequence under the output root; requires a Maya GUI viewport."""
    return await invoke("playblast", {"path": path, "start": start, "end": end, "width": width, "height": height})


@mcp.tool()
async def capture_frame(path: str, frame: float | None = None, width: int = 960, height: int = 540) -> Image:
    """Capture a Maya viewport PNG and return pixels to the agent for visual inspection."""
    result = await invoke("capture_frame", {"path": path, "frame": frame, "width": width, "height": height})
    output = Path(result["path"])
    if output.stat().st_size > 20 * 1024 * 1024:
        raise ToolError("Captured image exceeds 20 MiB")
    return Image(data=output.read_bytes(), format="png")


@mcp.tool()
async def execute_python(code: str, undo_chunk: bool = True) -> dict:
    """Trusted Maya Python. Set undo_chunk=False for cmds.undo/redo or self-managed transactions; those calls do not auto-rollback."""
    return await invoke("execute_python", {"code": code, "undo_chunk": undo_chunk})


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
