"""Local, authenticated JSON bridge for a running Maya session.

Run ``start`` inside Maya's main thread.  Network workers never call maya.cmds.
This is an automation bridge, not a Python sandbox; arbitrary Python is opt-in.
"""

from __future__ import print_function

import contextlib
import hmac
import io
import json
import math
import os
import secrets
import socketserver
import sys
import threading

PROTOCOL = 1
BRIDGE_VERSION = "0.2.0"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 30.0
_server = None
_server_thread = None
_connection_file = None
_output_root = None
_token = None
_allow_python = False
_defer = None
_session_generation = 0
_state_lock = threading.RLock()


class BridgeError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


def _error(code, message):
    raise BridgeError(code, message)


def _cmds():
    import maya.cmds
    return maya.cmds


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _error("INVALID_ARGUMENT", label + " must be a finite number")
    if not math.isfinite(value):
        _error("INVALID_ARGUMENT", label + " must be a finite number")
    return value


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _error("INVALID_ARGUMENT", "%s must be an integer in %s..%s" % (label, minimum, maximum))
    return value


def _text(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        _error("INVALID_ARGUMENT", label + " must be a nonempty string")
    return value


def _nodes(values, allow_empty=False):
    if not isinstance(values, list) or (not values and not allow_empty):
        _error("INVALID_ARGUMENT", "nodes must be a list" + ("" if allow_empty else " with at least one node"))
    if len(values) > 1000:
        _error("INVALID_ARGUMENT", "at most 1000 nodes per request")
    cmds = _cmds()
    result = []
    for value in values:
        _text(value, "node")
        matches = cmds.ls(value, long=True) or []
        if len(matches) != 1 or matches[0] != value or not value.startswith("|"):
            _error("INVALID_NODE", "use one existing, exact full DAG path: " + value)
        if not cmds.objectType(value, isAType="dagNode"):
            _error("INVALID_NODE", "node is not a DAG node: " + value)
        if value not in result:
            result.append(value)
    return result


def _plug(node, attribute):
    _nodes([node])
    _text(attribute, "attribute")
    if "." in attribute or "[" in attribute or "]" in attribute:
        _error("INVALID_ATTRIBUTE", "use one scalar attribute name")
    cmds = _cmds()
    if not cmds.attributeQuery(attribute, node=node, exists=True):
        _error("INVALID_ATTRIBUTE", "attribute does not exist: " + node + "." + attribute)
    return node + "." + attribute


_NUMERIC_TYPES = {"double", "float", "doubleLinear", "doubleAngle", "time", "long", "short", "byte", "bool", "enum"}
_TANGENTS = {"auto", "clamped", "fast", "flat", "linear", "plateau", "slow", "spline", "step", "stepnext"}


def _writable_plug(node, attribute):
    cmds = _cmds()
    plug = _plug(node, attribute)
    kind = cmds.getAttr(plug, type=True)
    if kind not in _NUMERIC_TYPES:
        _error("INVALID_ATTRIBUTE", "attribute must be scalar numeric: " + plug)
    if cmds.getAttr(plug, lock=True):
        _error("LOCKED_ATTRIBUTE", "attribute is locked: " + plug)
    if not cmds.attributeQuery(attribute, node=node, writable=True):
        _error("READ_ONLY_ATTRIBUTE", "attribute is read-only: " + plug)
    sources = cmds.listConnections(plug, source=True, destination=False, plugs=True) or []
    for source in sources:
        source_type = cmds.nodeType(source.split(".", 1)[0])
        if not (source_type.startswith("animCurveT") or source_type.startswith("animBlendNode")):
            _error("CONNECTED_ATTRIBUTE", "attribute has a non-animation input: " + plug)
        if source_type.startswith("animBlendNode"):
            history = cmds.listHistory(source.split(".", 1)[0], pruneDagObjects=True) or []
            if any(cmds.nodeType(n).startswith("animCurveU") for n in history):
                _error("CONNECTED_ATTRIBUTE", "attribute is driven by a unitless/set-driven-key curve: " + plug)
    if not sources and not cmds.getAttr(plug, settable=True):
        _error("READ_ONLY_ATTRIBUTE", "attribute cannot be set: " + plug)
    return plug, kind


def _value_for_type(value, kind, plug):
    _finite(value, "value for " + plug)
    if kind in {"long", "short", "byte", "bool", "enum"} and int(value) != value:
        _error("INVALID_ARGUMENT", "integer attribute needs an integer: " + plug)
    if kind == "bool" and value not in (0, 1):
        _error("INVALID_ARGUMENT", "boolean attribute needs 0 or 1: " + plug)
    if kind == "enum":
        node, attribute = plug.rsplit(".", 1)
        raw = (_cmds().attributeQuery(attribute, node=node, listEnum=True) or [""])[0]
        numbers = []
        index = 0
        for item in raw.split(":"):
            if "=" in item:
                index = int(item.rsplit("=", 1)[1])
            numbers.append(index)
            index += 1
        if value not in numbers:
            _error("INVALID_ARGUMENT", "invalid enum value: " + plug)
    return value


def _output_path(path, suffix=None):
    _text(path, "path")
    if not os.path.isabs(path):
        _error("INVALID_PATH", "output path must be absolute")
    root = os.path.normcase(os.path.realpath(_output_root))
    resolved = os.path.realpath(path)
    normalized = os.path.normcase(resolved)
    try:
        inside = os.path.commonpath([root, normalized]) == root and normalized != root
    except ValueError:
        inside = False
    if not inside:
        _error("INVALID_PATH", "output path must be inside the configured output_root")
    if suffix and not resolved.lower().endswith(suffix):
        _error("INVALID_PATH", "output path must end in " + suffix)
    if os.path.exists(resolved):
        _error("OUTPUT_EXISTS", "output already exists: " + resolved)
    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        _error("INVALID_PATH", "output parent directory does not exist")
    return resolved


@contextlib.contextmanager
def _undo_chunk(label):
    cmds = _cmds()
    old_state = cmds.undoInfo(query=True, state=True)
    # Restore undo state even if Maya refuses to open/close a chunk.
    try:
        if not old_state:
            cmds.undoInfo(stateWithoutFlush=True)
        chunk_name = label + "_" + secrets.token_hex(6)
        cmds.undoInfo(openChunk=True, chunkName=chunk_name)
        failed = False
        try:
            yield
        except Exception:
            failed = True
            raise
        finally:
            cmds.undoInfo(closeChunk=True)
            if failed and cmds.undoInfo(query=True, undoName=True) == chunk_name:
                cmds.undo()
    finally:
        if not old_state:
            cmds.undoInfo(stateWithoutFlush=False)


def _layer(name):
    cmds = _cmds()
    _text(name, "layer")
    if not cmds.objExists(name) or cmds.nodeType(name) != "animLayer":
        _error("INVALID_LAYER", "animation layer does not exist: " + name)
    base = cmds.animLayer(query=True, root=True)
    if name == base or name == "BaseAnimation":
        _error("INVALID_LAYER", "choose a non-base animation layer")
    if cmds.getAttr(name + ".lock") or cmds.referenceQuery(name, isNodeReferenced=True):
        _error("LOCKED_LAYER", "animation layer is locked or referenced: " + name)
    return name


def _get_status(params):
    cmds = _cmds()
    return {"protocol": PROTOCOL, "bridge_version": BRIDGE_VERSION,
            "maya_version": cmds.about(version=True), "batch": bool(cmds.about(batch=True)),
            "python_version": sys.version.split()[0], "pid": os.getpid(),
            "scene": cmds.file(query=True, sceneName=True),
            "allow_python": _allow_python, "output_root": _output_root,
            "scheduler": "standalone_queue" if _defer is not None else "maya_executeDeferred",
            "methods": sorted(_METHODS)}


def _get_scene_info(params):
    cmds = _cmds()
    return {"scene": cmds.file(query=True, sceneName=True), "modified": cmds.file(query=True, modified=True),
            "time": cmds.currentTime(query=True), "time_unit": cmds.currentUnit(query=True, time=True),
            "linear_unit": cmds.currentUnit(query=True, linear=True), "up_axis": cmds.upAxis(query=True, axis=True),
            "playback": {"start": cmds.playbackOptions(query=True, minTime=True),
                         "end": cmds.playbackOptions(query=True, maxTime=True),
                         "animation_start": cmds.playbackOptions(query=True, animationStartTime=True),
                         "animation_end": cmds.playbackOptions(query=True, animationEndTime=True)},
            "selection": cmds.ls(selection=True, long=True) or [],
            "namespaces": sorted(cmds.namespaceInfo(listOnlyNamespaces=True, recurse=True) or []),
            "references": _references(),
            "animation_layers": [{"name": layer, "locked": bool(cmds.getAttr(layer + ".lock")),
                                   "muted": bool(cmds.getAttr(layer + ".mute"))}
                                  for layer in cmds.ls(type="animLayer") or []]}


def _references():
    cmds = _cmds()
    references = []
    for ref in cmds.ls(type="reference") or []:
        if ref == "sharedReferenceNode":
            continue
        try:
            references.append({"node": ref, "path": cmds.referenceQuery(ref, filename=True),
                               "loaded": bool(cmds.referenceQuery(ref, isLoaded=True)),
                               "namespace": cmds.referenceQuery(ref, namespace=True)})
        except RuntimeError:
            continue
    return references


def _discover_rig(params):
    cmds = _cmds()
    root = params.get("root")
    limit = _integer(params.get("limit", 100), "limit", 1, 1000)
    offset = _integer(params.get("offset", 0), "offset", 0, 10000000)
    if root is not None:
        _nodes([root])
        candidates = [root] + (cmds.listRelatives(root, allDescendents=True, fullPath=True) or [])
        shapes = [n for n in candidates if cmds.nodeType(n) == "nurbsCurve"]
        joints = [n for n in candidates if cmds.nodeType(n) == "joint"]
    else:
        shapes = cmds.ls(type="nurbsCurve", long=True) or []
        joints = cmds.ls(type="joint", long=True) or []
    controls = sorted(set(p for s in shapes for p in (cmds.listRelatives(s, parent=True, fullPath=True) or [])))
    return {"root": root, "controls": controls[offset:offset + limit], "total_controls": len(controls),
            "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < len(controls) else None,
            "joint_count": len(joints), "joint_sample": sorted(joints)[:100],
            "namespaces": sorted(cmds.namespaceInfo(listOnlyNamespaces=True, recurse=True) or []),
            "references": _references(), "note": "NURBS curve transforms are candidates; semantic rig mapping is not inferred."}


def _inspect_nodes(params):
    cmds = _cmds()
    result = []
    for node in _nodes(params.get("nodes")):
        attrs = []
        for attr in sorted(cmds.listAttr(node, keyable=True) or []):
            plug = node + "." + attr
            item = {"name": attr, "type": cmds.getAttr(plug, type=True),
                    "keyable": bool(cmds.getAttr(plug, keyable=True)), "locked": bool(cmds.getAttr(plug, lock=True)),
                    "settable": bool(cmds.getAttr(plug, settable=True)),
                    "incoming": cmds.listConnections(plug, source=True, destination=False, plugs=True) or []}
            if item["type"] == "enum":
                item["enum"] = cmds.attributeQuery(attr, node=node, listEnum=True) or []
            attrs.append(item)
        constraints = sorted(set(cmds.listConnections(node, source=True, destination=False, type="constraint") or []))
        result.append({"node": node, "type": cmds.nodeType(node),
                       "referenced": bool(cmds.referenceQuery(node, isNodeReferenced=True)),
                       "attributes": attrs, "constraints": constraints})
    return {"nodes": result}


def _get_pose(params):
    cmds = _cmds()
    nodes = _nodes(params.get("nodes"))
    frame = params.get("frame")
    if frame is not None:
        _finite(frame, "frame")
    old_time = cmds.currentTime(query=True)
    old_undo = cmds.undoInfo(query=True, state=True)
    result = []
    # Maya 2022 records currentTime edits (including restore) as unnamed undo
    # entries. A read-only pose inspection must not hide the preceding edit.
    try:
        if old_undo:
            cmds.undoInfo(stateWithoutFlush=False)
        cmds.currentTime(old_time if frame is None else frame, edit=True, update=True)
        for node in nodes:
            attributes = {}
            for attr in sorted(cmds.listAttr(node, keyable=True, scalar=True) or []):
                plug = node + "." + attr
                if cmds.getAttr(plug, type=True) in _NUMERIC_TYPES:
                    attributes[attr] = cmds.getAttr(plug)
            result.append({"node": node, "attributes": attributes,
                           "world_matrix": cmds.xform(node, query=True, worldSpace=True, matrix=True)})
    finally:
        try:
            if cmds.currentTime(query=True) != old_time:
                cmds.currentTime(old_time, edit=True, update=True)
        finally:
            if old_undo:
                cmds.undoInfo(stateWithoutFlush=True)
    return {"frame": old_time if frame is None else frame, "nodes": result}


def _set_keyframes(params):
    cmds = _cmds()
    keys = params.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 10000:
        _error("INVALID_ARGUMENT", "keys must contain 1..10000 entries")
    itt, ott = params.get("in_tangent", "auto"), params.get("out_tangent", "auto")
    if itt not in _TANGENTS or ott not in _TANGENTS or itt in {"step", "stepnext"}:
        _error("INVALID_ARGUMENT", "unsupported tangent type")
    layer = params.get("layer")
    layer = _layer(layer) if layer is not None else None
    if layer is None and any(n != cmds.animLayer(query=True, root=True) for n in cmds.ls(type="animLayer") or []):
        _error("LAYER_REQUIRED", "scene has animation layers; specify an existing writable non-base layer explicitly")
    checked = []
    members = set(cmds.animLayer(layer, query=True, attribute=True) or []) if layer else set()
    for key in keys:
        if not isinstance(key, dict):
            _error("INVALID_ARGUMENT", "each key must be an object")
        node, attr = key.get("node"), key.get("attribute")
        plug, kind = _writable_plug(node, attr)
        time = _finite(key.get("time"), "time")
        value = _value_for_type(key.get("value"), kind, plug)
        checked.append((node, attr, plug, time, value))
    old_time = cmds.currentTime(query=True)
    try:
        with _undo_chunk("MayaMCP_set_keyframes"):
            if layer:
                for plug in sorted(set(k[2] for k in checked)):
                    if plug not in members:
                        cmds.animLayer(layer, edit=True, attribute=plug)
            for node, attr, plug, time, value in checked:
                options = {"attribute": attr, "time": time, "value": value,
                           "inTangentType": itt, "outTangentType": ott}
                if layer:
                    options["animLayer"] = layer
                if not cmds.setKeyframe(node, **options):
                    _error("KEYFRAME_FAILED", "Maya did not create a key: " + plug)
    finally:
        if cmds.currentTime(query=True) != old_time:
            cmds.currentTime(old_time, edit=True)
    return {"key_count": len(checked), "layer": layer, "time_restored": old_time}


def _create_animation_layer(params):
    cmds = _cmds()
    name = _text(params.get("name"), "name")
    if any(c in name for c in "|.[] *?\t\r\n") or name == "BaseAnimation":
        _error("INVALID_ARGUMENT", "invalid animation layer name")
    if cmds.objExists(name):
        _error("LAYER_EXISTS", "a node with that name already exists")
    nodes = _nodes(params.get("nodes", []), allow_empty=True)
    attrs = params.get("attributes", ["translateX", "translateY", "translateZ", "rotateX", "rotateY", "rotateZ"])
    if not isinstance(attrs, list) or not attrs or len(attrs) > 100:
        _error("INVALID_ARGUMENT", "attributes must contain 1..100 scalar names")
    override = params.get("override", False)
    if not isinstance(override, bool):
        _error("INVALID_ARGUMENT", "override must be a boolean")
    plugs = [_writable_plug(node, attr)[0] for node in nodes for attr in attrs]
    with _undo_chunk("MayaMCP_create_animation_layer"):
        created = cmds.animLayer(name, override=override)
        if created != name:
            _error("LAYER_FAILED", "Maya changed the requested layer name")
        for plug in sorted(set(plugs)):
            cmds.animLayer(created, edit=True, attribute=plug)
    return {"layer": created, "override": override, "attributes": sorted(set(plugs))}


def _get_animation_curves(params):
    cmds = _cmds()
    nodes = _nodes(params.get("nodes"))
    start, end = params.get("start"), params.get("end")
    if start is not None:
        _finite(start, "start")
    if end is not None:
        _finite(end, "end")
    if start is not None and end is not None and end < start:
        _error("INVALID_ARGUMENT", "end must be greater than or equal to start")
    result = []
    for node in nodes:
        # keyframe(node, q=True, name=True) only returns Maya's best/active layer.
        # Walk the standard animation input graph to include BaseAnimation and
        # every layer without walking constraints into other rig controls.
        pending = list(cmds.listConnections(node, source=True, destination=False) or [])
        seen, curves = set(), set()
        while pending:
            upstream = pending.pop()
            if upstream in seen:
                continue
            seen.add(upstream)
            node_type = cmds.nodeType(upstream)
            if node_type.startswith("animCurve"):
                curves.add(upstream)
            elif node_type.startswith("animBlendNode") or node_type in {"unitConversion", "pairBlend", "blendWeighted"}:
                pending.extend(cmds.listConnections(upstream, source=True, destination=False) or [])
        for curve in sorted(curves):
            domain = "unitless" if cmds.nodeType(curve).startswith("animCurveU") else "time"
            times = cmds.keyframe(curve, query=True, **({"floatChange": True} if domain == "unitless" else {"timeChange": True})) or []
            values = cmds.keyframe(curve, query=True, valueChange=True) or []
            itt = cmds.keyTangent(curve, query=True, inTangentType=True) or []
            ott = cmds.keyTangent(curve, query=True, outTangentType=True) or []
            keys = [{"input" if domain == "unitless" else "time": t, "value": v, "in_tangent": itt[i], "out_tangent": ott[i]}
                    for i, (t, v) in enumerate(zip(times, values))
                    if domain == "unitless" or ((start is None or t >= start) and (end is None or t <= end))]
            result.append({"node": node, "curve": curve, "type": cmds.nodeType(curve), "keys": keys,
                           "input_domain": domain,
                           "outputs": cmds.listConnections(curve + ".output", source=False, destination=True, plugs=True) or []})
    return {"curves": result}


def _set_current_time(params):
    frame = _finite(params.get("frame"), "frame")
    _cmds().currentTime(frame, edit=True)
    return {"frame": _cmds().currentTime(query=True)}


def _save_scene_copy(params):
    cmds = _cmds()
    path = _output_path(params.get("path"), ".ma")
    original_name = cmds.file(query=True, sceneName=True)
    modified = cmds.file(query=True, modified=True)
    try:
        # Export all preserves the user's current file name, file format,
        # rename-to-save state and references, including unnamed scenes.
        # Save+rename would silently change a .mb current scene into ASCII.
        cmds.file(path, exportAll=True, type="mayaAscii", force=False,
                  preserveReferences=True, options="v=0;")
    finally:
        cmds.file(modified=modified)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        _error("SAVE_FAILED", "Maya did not write a nonempty scene copy; inspect the output before retrying")
    return {"path": path, "original_scene": original_name, "original_modified": modified,
            "mode": "export_all_preserving_references"}


def _validate_png(path):
    try:
        with open(path, "rb") as stream:
            signature = stream.read(24)
    except OSError:
        _error("CAPTURE_FAILED", "Maya did not write a readable PNG; an active viewport is required")
    if len(signature) < 24 or signature[:8] != b"\x89PNG\r\n\x1a\n" or signature[12:16] != b"IHDR":
        _error("CAPTURE_FAILED", "Maya output has no valid PNG header; partial output is retained: " + path)


def _playblast(params, single=False):
    cmds = _cmds()
    if cmds.about(batch=True):
        _error("GUI_REQUIRED", "playblast/capture_frame requires Maya GUI with an active viewport")
    path = _output_path(params.get("path"), ".png" if single else None)
    width = _integer(params.get("width", 960), "width", 16, 8192)
    height = _integer(params.get("height", 540), "height", 16, 8192)
    old_time = cmds.currentTime(query=True)
    selection = cmds.ls(selection=True, long=True) or []
    options = {"format": "image", "compression": "png", "viewer": False,
               "forceOverwrite": False, "widthHeight": (width, height), "percent": 100,
               "showOrnaments": False, "offScreen": True, "quality": 100}
    if single:
        frame = params.get("frame")
        frame = old_time if frame is None else _finite(frame, "frame")
        options.update({"completeFilename": path, "frame": frame})
    else:
        start = _finite(params.get("start"), "start")
        end = _finite(params.get("end"), "end")
        if end < start or end - start > 10000:
            _error("INVALID_ARGUMENT", "playblast needs start <= end with at most 10001 frames")
        # A dedicated directory avoids collisions with Maya's numbered output names.
        if os.listdir(os.path.dirname(path)):
            _error("OUTPUT_EXISTS", "playblast needs an empty output directory to avoid sequence collisions")
        options.update({"filename": path, "startTime": start, "endTime": end, "framePadding": 4})
    try:
        result = cmds.playblast(**options)
    finally:
        try:
            if cmds.currentTime(query=True) != old_time:
                cmds.currentTime(old_time, edit=True)
        finally:
            if selection:
                cmds.select(selection, replace=True)
            else:
                cmds.select(clear=True)
    if single:
        _validate_png(path)
        return {"path": path, "frame": frame, "width": width, "height": height}
    files = sorted(os.path.join(os.path.dirname(path), f) for f in os.listdir(os.path.dirname(path))
                   if f.lower().endswith(".png"))
    expected = int(math.floor(end - start)) + 1
    if len(files) != expected:
        _error("CAPTURE_INCOMPLETE", "Expected %d PNG frames, received %d; partial output is retained at %s" % (expected, len(files), os.path.dirname(path)))
    for filename in files:
        _validate_png(filename)
    return {"path": result, "files": files, "start": start, "end": end, "width": width, "height": height, "frame_count": len(files)}


def _execute_python(params):
    if not _allow_python:
        _error("PYTHON_DISABLED", "execute_python is disabled; start with allow_python=True to enable trusted code")
    code = _text(params.get("code"), "code")
    if len(code.encode("utf-8")) > 1024 * 1024:
        _error("INVALID_ARGUMENT", "code exceeds 1 MiB")
    undo_chunk = params.get("undo_chunk", True)
    if not isinstance(undo_chunk, bool):
        _error("INVALID_ARGUMENT", "undo_chunk must be a boolean")
    output, errors = io.StringIO(), io.StringIO()
    namespace = {"__name__": "__maya_mcp__", "cmds": _cmds()}
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            # Undo/redo commands must execute outside an open undo chunk.
            transaction = _undo_chunk("MayaMCP_execute_python") if undo_chunk else contextlib.nullcontext()
            with transaction:
                exec(compile(code, "<maya-mcp>", "exec"), namespace, namespace)
                result = namespace.get("result")
                json.dumps(result, allow_nan=False)
    except Exception as exc:
        _error("PYTHON_ERROR", "%s: %s" % (type(exc).__name__, exc))
    return {"stdout": output.getvalue(), "stderr": errors.getvalue(), "result": result,
            "warning": "Trusted Python, not a sandbox. Undo cannot reverse filesystem or other external side effects."}


_METHODS = {"get_status": _get_status, "get_scene_info": _get_scene_info, "discover_rig": _discover_rig,
            "inspect_nodes": _inspect_nodes, "get_pose": _get_pose, "set_keyframes": _set_keyframes,
            "create_animation_layer": _create_animation_layer, "get_animation_curves": _get_animation_curves,
            "set_current_time": _set_current_time, "save_scene_copy": _save_scene_copy,
            "playblast": _playblast, "capture_frame": lambda p: _playblast(p, single=True),
            "execute_python": _execute_python}


def _execute_method(method, params):
    """Main-thread dispatcher, also useful for native Maya standalone validation."""
    if not isinstance(method, str) or method not in _METHODS:
        _error("METHOD_NOT_FOUND", "unknown method")
    if not isinstance(params, dict):
        _error("INVALID_ARGUMENT", "params must be an object")
    return _METHODS[method](params)


def _dispatch(method, params, generation=None):
    event = threading.Event()
    lock = threading.Lock()
    job = {"state": "pending"}
    if generation is None:
        generation = _session_generation

    def run():
        with lock:
            if job["state"] == "cancelled":
                return
            if generation != _session_generation:
                job["error"] = ("SESSION_EXPIRED", "bridge session stopped or restarted; request was cancelled")
                job["state"] = "done"
                event.set()
                return
            job["state"] = "running"
        try:
            job["result"] = _execute_method(method, params)
        except BridgeError as exc:
            job["error"] = (exc.code, str(exc))
        except Exception as exc:
            job["error"] = ("MAYA_ERROR", "%s: %s" % (type(exc).__name__, exc))
        finally:
            with lock:
                job["state"] = "done"
            event.set()

    if _defer is None:
        import maya.utils
        maya.utils.executeDeferred(run)
    else:
        _defer(run)
    if not event.wait(REQUEST_TIMEOUT):
        with lock:
            state = job["state"]
            if state == "pending":
                job["state"] = "cancelled"
                _error("TIMEOUT_CANCELLED", "Maya did not start the request; it has been cancelled")
            if state != "done":
                _error("EXECUTION_UNKNOWN", "Maya started the request but has not finished; inspect scene state and do not automatically retry")
    if "error" in job:
        _error(*job["error"])
    return job.get("result")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request_id = None
        self.connection.settimeout(10.0)
        try:
            raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                _error("INVALID_REQUEST", "request must be one UTF-8 JSON line, at most 8 MiB")
            request = json.loads(raw.decode("utf-8"), parse_constant=lambda _: _error("INVALID_REQUEST", "JSON numbers must be finite"))
            if not isinstance(request, dict):
                _error("INVALID_REQUEST", "request must be an object")
            request_id = request.get("id")
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                _error("INVALID_REQUEST", "id must be a string or integer")
            supplied = request.get("token")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.server.auth_token):
                _error("UNAUTHORIZED", "invalid bridge token")
            result = _dispatch(request.get("method"), request.get("params", {}), self.server.generation)
            response = {"id": request_id, "ok": True, "result": result}
        except BridgeError as exc:
            response = {"id": request_id, "ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except (ValueError, UnicodeError):
            response = {"id": request_id, "ok": False, "error": {"code": "INVALID_REQUEST", "message": "invalid UTF-8 JSON"}}
        except Exception as exc:
            response = {"id": request_id, "ok": False, "error": {"code": "BRIDGE_ERROR", "message": type(exc).__name__}}
        try:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if len(encoded) > MAX_MESSAGE_BYTES:
                encoded = (json.dumps({"id": request_id, "ok": False, "error": {"code": "RESPONSE_TOO_LARGE", "message": "response exceeds 8 MiB; use smaller queries"}}) + "\n").encode("utf-8")
            self.wfile.write(encoded)
        except (OSError, ValueError):
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 16


def start(connection_file, port=0, output_root=None, allow_python=False, defer=None):
    """Start loopback bridge. Call inside Maya; does not open or create scenes.

    The connection file contains a local capability token. Keep it private.
    ``allow_python=True`` grants trusted arbitrary code execution in Maya.
    """
    global _server, _server_thread, _connection_file, _output_root, _token, _allow_python, _session_generation, _defer
    with _state_lock:
        if _server is not None:
            _error("ALREADY_RUNNING", "bridge is already running; stop it before restarting")
        _text(connection_file, "connection_file")
        if not os.path.isabs(connection_file):
            _error("INVALID_PATH", "connection_file must be absolute")
        if output_root is None or not isinstance(output_root, str) or not os.path.isabs(output_root) or not os.path.isdir(output_root):
            _error("INVALID_PATH", "output_root must be an existing absolute directory")
        _integer(port, "port", 0, 65535)
        if not isinstance(allow_python, bool):
            _error("INVALID_ARGUMENT", "allow_python must be a boolean")
        if defer is not None and not callable(defer):
            _error("INVALID_ARGUMENT", "defer must be a callable scheduler or None")
        parent = os.path.dirname(os.path.abspath(connection_file))
        if not os.path.isdir(parent):
            _error("INVALID_PATH", "connection_file parent directory does not exist")
        server = _Server(("127.0.0.1", port), _Handler)
        token = secrets.token_hex(32)
        server.auth_token = token
        metadata = {"host": "127.0.0.1", "port": server.server_address[1], "token": token,
                    "protocol": PROTOCOL, "pid": os.getpid()}
        temporary = connection_file + ".tmp-" + secrets.token_hex(6)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False)
                stream.write("\n")
            os.replace(temporary, connection_file)
        except Exception:
            server.server_close()
            if os.path.exists(temporary):
                os.remove(temporary)
            raise
        _server = server
        _session_generation += 1
        server.generation = _session_generation
        _connection_file = connection_file
        _output_root = os.path.realpath(output_root)
        _token = token
        _allow_python = allow_python
        _defer = defer
        _server_thread = threading.Thread(target=server.serve_forever, name="MayaAnimationMCP", daemon=True)
        _server_thread.start()
        return metadata


def stop():
    """Stop accepting new requests; a running Maya operation finishes normally."""
    global _server, _server_thread, _connection_file, _token, _allow_python, _session_generation, _defer
    with _state_lock:
        if _server is None:
            return {"stopped": False}
        server = _server
        _server = None
        _session_generation += 1
        server.shutdown()
        server.server_close()
        if _connection_file and os.path.isfile(_connection_file):
            try:
                with open(_connection_file, "r", encoding="utf-8") as stream:
                    stored = json.load(stream)
                if stored.get("token") == _token:
                    os.remove(_connection_file)
            except (OSError, ValueError):
                pass
        _server_thread = None
        _connection_file = None
        _token = None
        _allow_python = False
        _defer = None
        return {"stopped": True}
