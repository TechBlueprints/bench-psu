# SPDX-License-Identifier: Apache-2.0
"""A minimal MCP server (Streamable HTTP, JSON responses only) inside the daemon.

Standard library only: JSON-RPC 2.0 over ``POST /mcp``, answering with
``application/json`` (no SSE stream, no server-initiated messages). It shares
the daemon's worker, so the one-owner rule for the serial port still holds.

Read tools are open. Write tools are listed and callable only when the
request carries the daemon's bearer token; without it the server presents a
read-only toolset.
"""

from __future__ import annotations

import json

from . import __version__

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
Controls one Kiprim DC620S bench power supply (0-60 V, 0-20 A, 400 W) through the
bench-psud daemon that owns its USB port.

Safety rules, which the daemon enforces where it can and you must follow where it cannot:
- Call get_status first and read 'output', 'setpoints', 'protection', 'ceilings' and 'warnings'.
- Never assume what is connected to the terminals. Ask the human before switching the output on
  or raising a setpoint, and say what you will set.
- Never connect to or charge a battery with this supply: it has no reverse-current blocking and
  the manual forbids direct battery charging.
- Changing the current limit while the output is on switches the output off and back on
  (firmware can trip OCP on a live write). Say so before doing it.
- Never raise the OVP limit to silence an OVP trip; report the trip instead.
- Host ceilings (max_voltage / max_current) are hard limits set by the owner; do not try to work
  around them.
"""


def _tool(name, description, schema, read_only, destructive=False):
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                        "idempotentHint": True, "openWorldHint": False},
    }


def tools(writer: bool) -> list[dict]:
    from .daemon import OPS

    out = [
        _tool("get_status",
              "Current state of the supply: output on/off, mode (cv/cc/standby/fault), measured V/A/W, "
              "setpoints, OVP/OCP limits and trip flags, host ceilings, link health, identity, warnings.",
              {"type": "object", "properties": {}}, True),
    ]
    if writer:
        for name, (_, schema, description) in OPS.items():
            out.append(_tool(name, description, schema, False,
                             destructive=name in ("set_output", "set_current", "set_voltage_current")))
        out.append(_tool(
            "probe",
            "Run the conformance probe. level=queries changes nothing; setters needs the output OFF and "
            "writes each setpoint back unchanged plus a lock/unlock; output and ocp_quirk switch the "
            "output on and off (only with open terminals or a resistive load, and only when the human agreed).",
            {"type": "object", "properties": {"level": {"type": "string",
             "enum": ["queries", "setters", "output", "ocp_quirk"], "default": "queries"}}},
            False, destructive=True))
    return out


def _result(payload: dict, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}],
            "structuredContent": payload, "isError": is_error}


def _call(d, name: str, args: dict, writer: bool, source: str) -> dict:
    from .daemon import OPS, classify

    if name == "get_status":
        return _result(d.status())
    if name not in OPS and name != "probe":
        raise KeyError(name)
    if not writer:
        return _result({"ok": False, "error": "write tools need the bearer token "
                        "(Authorization: Bearer <token> on the MCP connection)"}, True)
    try:
        if name == "probe":
            rows = d.run_probe(str(args.get("level", "queries")), source)
            return _result({"ok": True, "rows": rows})
        result = d.run_op(name, args, source)
    except Exception as exc:
        _, kind = classify(exc)
        return _result({"ok": False, "kind": kind, "error": str(exc), "status": d.status()}, True)
    return _result({"ok": True, "result": result, "status": d.status()})


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _one(d, msg, writer: bool, source: str) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if msg_id is None:  # notification (e.g. notifications/initialized): no reply
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "bench-psu", "title": "Bench PSU (Kiprim DC620S)",
                           "version": __version__},
            "instructions": INSTRUCTIONS + ("" if writer else
                "\nThis connection has no token: only get_status is available."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools(writer)}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(msg_id, -32602, "arguments must be an object")
        try:
            return {"jsonrpc": "2.0", "id": msg_id, "result": _call(d, name, args, writer, source)}
        except KeyError:
            return _error(msg_id, -32602, f"unknown tool {name!r}")
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}
    return _error(msg_id, -32601, f"method not found: {method}")


def handle(d, body, writer: bool, source: str):
    """Answer one JSON-RPC message or a batch; None when nothing needs a reply."""
    if isinstance(body, list):
        replies = [r for r in (_one(d, m, writer, source) for m in body) if r is not None]
        return replies or None
    return _one(d, body, writer, source)
