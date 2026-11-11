# SPDX-License-Identifier: Apache-2.0
"""HTTP robustness and the DNS-rebinding guard (review 2026-11-11)."""
import http.client
import json
import socket
import time

from conftest import TOKEN

from kiprim.daemon import host_ok


def conn(r, timeout=3):
    host, port = r.base.split("//")[1].split(":")
    return http.client.HTTPConnection(host, int(port), timeout=timeout)


def raw(r, data: bytes, timeout=3) -> bytes:
    host, port = r.base.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out


def test_bad_content_length_is_400_and_closes(make_daemon):
    r = make_daemon()
    for value in (b"-5", b"abc", b"999999"):
        out = raw(r, b"POST /api/voltage HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: " + value + b"\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 400"), out[:80]
        assert b"Connection: close" in out
    assert r.supply.setters_received() == []


def test_refusal_keeps_keepalive_in_step(make_daemon):
    r = make_daemon()
    c = conn(r)
    c.request("POST", "/api/output", body=json.dumps({"on": True}),
              headers={"Origin": "http://evil.example", "Content-Type": "application/json"})
    r1 = c.getresponse()
    r1.read()
    c.request("GET", "/api/health")
    r2 = c.getresponse()
    r2.read()
    assert (r1.status, r2.status) == (403, 200)
    c.request("POST", "/api/output", body=json.dumps({"on": True}),
              headers={"Content-Type": "application/json"})  # no token
    r3 = c.getresponse()
    r3.read()
    c.request("GET", "/api/status")
    r4 = c.getresponse()
    assert (r3.status, r4.status) == (401, 200)
    assert r.supply.setters_received() == []


def test_unknown_host_header_refused_for_reads_and_writes(make_daemon):
    r = make_daemon()
    for method, path in (("GET", "/api/status"), ("GET", "/"), ("POST", "/api/output")):
        c = conn(r)
        c.request(method, path, body=b"{}" if method == "POST" else None,
                  headers={"Host": "attacker.example:8100", "Authorization": f"Bearer {TOKEN}"})
        resp = c.getresponse()
        assert resp.status == 403, (method, path)
    assert r.supply.setters_received() == []


def test_allowed_hosts(make_daemon):
    r = make_daemon(allowed_hosts=["bench-psu.example.lan"])
    c = conn(r)
    c.request("GET", "/api/health", headers={"Host": "bench-psu.example.lan:8100"})
    assert c.getresponse().status == 200


def test_host_ok_rules():
    names = {"localhost", "bench-psu", "bench-psu.local.example"}
    assert host_ok("10.0.106.223:8100", names)
    assert host_ok("[::1]:8100", names)
    assert host_ok("bench-psu", names)
    assert host_ok("BENCH-PSU.local.example.", names)
    assert host_ok(None, names)
    assert not host_ok("evil.example", names)
    assert not host_ok("bench-psu.evil.example:8100", names)


def test_mcp_wrong_token_is_refused_not_downgraded(make_daemon):
    r = make_daemon()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    code, body = r.req("POST", "/mcp", msg, token="wrong")
    assert code == 403
    code, body = r.req("POST", "/mcp", msg)  # no header at all: read-only
    assert code == 200 and [t["name"] for t in body["result"]["tools"]] == ["get_status"]
    code, body = r.req("POST", "/mcp", msg, token=TOKEN)
    assert "reset_counters" in [t["name"] for t in body["result"]["tools"]]


def test_stalled_client_is_dropped(make_daemon):
    r = make_daemon(request_timeout=0.5)
    host, port = r.base.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=3)
    s.sendall(b"POST /api/output HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 50\r\n\r\n{")  # body never finishes
    t0 = time.monotonic()
    data = s.recv(100)  # server gives up and closes
    assert data == b"" and time.monotonic() - t0 < 2.5
    assert r.supply.setters_received() == []
