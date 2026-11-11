# SPDX-License-Identifier: Apache-2.0
from conftest import TOKEN

from kiprim.simulator import SimulatedSupply


def rpc(r, method, params=None, token=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method, "id": msg_id}
    if params is not None:
        body["params"] = params
    return r.req("POST", "/mcp", body, token=token)


def test_initialize_and_notification(make_daemon):
    r = make_daemon()
    code, body = rpc(r, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "0"}})
    assert code == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    assert "battery" in body["result"]["instructions"]
    code, _ = r.req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert code == 202


def test_read_only_without_token(make_daemon):
    r = make_daemon()
    _, body = rpc(r, "tools/list")
    assert [t["name"] for t in body["result"]["tools"]] == ["get_status"]
    _, body = rpc(r, "tools/call", {"name": "set_output", "arguments": {"on": True}})
    assert body["result"]["isError"] is True
    assert r.supply.setters_received() == []


def test_writer_tools_with_token(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    _, body = rpc(r, "tools/list", token=TOKEN)
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"get_status", "set_voltage_current", "set_output", "set_ovp", "probe"} <= names
    _, body = rpc(r, "tools/call", {"name": "set_voltage_current",
                                    "arguments": {"volts": 12, "amps": 1}}, token=TOKEN)
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["result"]["voltage"] == 12.0
    _, body = rpc(r, "tools/call", {"name": "set_voltage", "arguments": {"volts": 40}}, token=TOKEN)
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["kind"] == "limit"


def test_get_status_tool(make_daemon):
    r = make_daemon()
    _, body = rpc(r, "tools/call", {"name": "get_status", "arguments": {}})
    assert body["result"]["structuredContent"]["identity"]["model"] == "DC620S"


def test_unknown_method_and_tool(make_daemon):
    r = make_daemon()
    assert rpc(r, "nope")[1]["error"]["code"] == -32601
    assert rpc(r, "tools/call", {"name": "nope"}, token=TOKEN)[1]["error"]["code"] == -32602


def test_get_mcp_is_405(make_daemon):
    assert make_daemon().req("GET", "/mcp")[0] == 405
