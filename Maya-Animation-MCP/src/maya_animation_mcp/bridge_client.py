"""One request per loopback connection. Never retry a possibly executed write."""
import json
import os
from pathlib import Path
import socket
import uuid

MAX_BYTES = 8 * 1024 * 1024


class BridgeError(RuntimeError):
    pass


def connection_path() -> Path:
    default = Path(__file__).resolve().parents[2] / "runtime" / "connection.json"
    return Path(os.environ.get("MAYA_MCP_CONNECTION", str(default)))


def _invalid_number(value):
    raise ValueError("Non-finite JSON number")


def call(method: str, params: dict | None = None) -> dict:
    path = connection_path()
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError(f"CONNECTION_NOT_READY: Run maya_side/start_in_maya.py in Maya. Connection file: {path}") from exc
    if not isinstance(info, dict):
        raise BridgeError("INVALID_CONNECTION: connection file must contain an object")
    if info.get("host") != "127.0.0.1" or type(info.get("protocol")) is not int or info["protocol"] != 1:
        raise BridgeError("INVALID_CONNECTION: unsupported bridge protocol or non-loopback host")
    port, token = info.get("port"), info.get("token")
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
        raise BridgeError("INVALID_CONNECTION: port or session token is missing or invalid; restart the Maya bridge")
    request_id = uuid.uuid4().hex
    request = {"id": request_id, "token": token, "method": method, "params": {} if params is None else params}
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (ValueError, TypeError) as exc:
        raise BridgeError("INVALID_ARGUMENT: request must be finite JSON data") from exc
    if len(payload) > MAX_BYTES:
        raise BridgeError("Request exceeds 8 MiB")
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError as exc:
        raise BridgeError("CONNECT_FAILED: Maya bridge is unreachable before sending the request. Check that Maya is open and its bridge is running; the connection file may be stale.") from exc
    try:
        with sock:
            sock.settimeout(40)
            sock.sendall(payload)
            with sock.makefile("rb") as stream:
                raw = stream.readline(MAX_BYTES + 1)
    except OSError as exc:
        raise BridgeError("EXECUTION_UNKNOWN: connection failed or timed out after sending began. Maya may be busy or blocked by a dialog; inspect its state before repeating a write.") from exc
    if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
        raise BridgeError("INVALID_RESPONSE: missing newline or oversized response; inspect Maya before repeating a write")
    try:
        response = json.loads(raw, parse_constant=_invalid_number)
    except (ValueError, UnicodeError) as exc:
        raise BridgeError("INVALID_RESPONSE: invalid finite UTF-8 JSON; inspect Maya before repeating a write") from exc
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise BridgeError("INVALID_RESPONSE: response must be an object with a boolean ok field; inspect Maya before repeating a write")
    if response.get("id") != request_id:
        raise BridgeError("INVALID_RESPONSE: response id mismatch; inspect Maya before repeating a write")
    if not response["ok"]:
        error = response.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            raise BridgeError("INVALID_RESPONSE: malformed error; inspect Maya before repeating a write")
        raise BridgeError(f"{error['code']}: {error['message']}")
    if not isinstance(response.get("result"), dict):
        raise BridgeError("INVALID_RESPONSE: missing object result; inspect Maya before repeating a write")
    return response["result"]
