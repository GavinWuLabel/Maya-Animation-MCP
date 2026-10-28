"""Isolated test scene. Never import this module into a production Maya session."""
import os
import sys
import time
import json

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
GUI = os.environ.get("MAYA_MCP_TEST_GUI") == "1"
if not GUI:
    import maya.standalone
    maya.standalone.initialize(name="python")
import maya.cmds as cmds
import maya.utils
from maya_side import maya_bridge

# Standalone has no GUI idle event loop. The test-only scheduler drains on this
# process's main thread; GUI/live tests use Maya's real executeDeferred unchanged.
if not GUI:
    import queue
    pending = queue.Queue()
    maya.utils.executeDeferred = pending.put

MODE = "gui" if GUI else "native"
OUTPUT = os.path.join(PROJECT, "evidence", MODE, os.environ.get("MAYA_MCP_TEST_RUN", ""))
os.makedirs(OUTPUT, exist_ok=True)
cmds.file(new=True, force=True)
cmds.currentUnit(time="film", linear="cm")
cmds.playbackOptions(minTime=1, maxTime=24)
ctrl = cmds.circle(name="MCP_TEST_CTRL", normal=(0, 1, 0), radius=2)[0]
cmds.addAttr(ctrl, longName="ikFk", attributeType="enum", enumName="FK:IK", keyable=True)
cmds.setAttr(ctrl + ".scaleX", lock=True)
cmds.select(clear=True)
joint = cmds.joint(name="MCP_TEST_JOINT", position=(0, 0, 0))
mesh = cmds.polySphere(name="MCP_TEST_MESH", radius=1.2, subdivisionsX=20, subdivisionsY=12)[0]
cmds.skinCluster(joint, mesh, toSelectedBones=True)
cmds.parentConstraint(ctrl, joint, maintainOffset=True)
cmds.setAttr(joint + ".visibility", 0)
cmds.currentTime(1)
cmds.select(ctrl)
if GUI:
    cmds.setAttr("persp.translate", 7, 5, 9, type="double3")
    cmds.setAttr("persp.rotate", -22, 36, 0, type="double3")
    cmds.viewFit("persp", all=True)
    for panel in cmds.getPanel(type="modelPanel") or []:
        cmds.modelEditor(panel, edit=True, displayAppearance="smoothShaded", displayTextures=True, grid=True)
    cmds.setFocus("modelPanel4")
    cmds.refresh(force=True)

maya_bridge.start(connection_file=os.path.join(OUTPUT, "connection.json"), output_root=OUTPUT, allow_python=False)
with open(os.path.join(OUTPUT, "ready.json"), "w") as stream:
    json.dump({"pid": os.getpid(), "gui": GUI, "control": "|" + ctrl, "joint": "|" + joint}, stream)
if not GUI:
    stop = os.path.join(OUTPUT, "stop")
    deadline = time.time() + 600
    while time.time() < deadline and not os.path.exists(stop):
        try:
            callback = pending.get(timeout=0.01)
        except queue.Empty:
            continue
        callback()
        time.sleep(0.005)
    maya_bridge.stop()
    maya.standalone.uninitialize()
