"""Run this file in Maya Script Editor (Python). It only starts the bridge."""
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
from maya_side import maya_bridge

runtime = os.path.join(PROJECT, "runtime")
output = os.path.abspath(os.environ.get("MAYA_MCP_OUTPUT_ROOT", os.path.join(PROJECT, "output")))
for directory in (runtime, output):
    if not os.path.isdir(directory):
        os.makedirs(directory)
info = maya_bridge.start(
    connection_file=os.path.join(runtime, "connection.json"),
    output_root=output,
    allow_python=False,
)
print("Maya Animation MCP ready on loopback port %s (protocol %s)" % (info["port"], info["protocol"]))
