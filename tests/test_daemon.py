# SPDX-License-Identifier: Apache-2.0
"""Daemon behaviour: the never-touch-the-output rule, polling, events, reconnect."""
import time

from conftest import TOKEN, wait_for

from kiprim.simulator import SimulatedSupply


def test_start_stop_never_sends_a_setter_output_on(make_daemon):
    s = SimulatedSupply(output=True, voltage=13.8, current=4.0)
    r = make_daemon(s)
    time.sleep(0.4)  # several polls and a full refresh
    r.stop()
    assert s.output is True
    assert s.setters_received() == []
    assert any(c == "MEAS:ALL:INFO?" for c in s.log)


def test_start_stop_never_sends_a_setter_output_off(make_daemon):
    s = SimulatedSupply(output=False)
    r = make_daemon(s)
    time.sleep(0.2)
    r.stop()
    assert s.output is False and s.setters_received() == []


def test_restart_leaves_output_unchanged(make_daemon):
    s = SimulatedSupply(output=True)
    for _ in range(3):
        make_daemon(s).stop()
    assert s.output is True and s.setters_received() == []


def test_output_off_on_exit_option(make_daemon):
    s = SimulatedSupply(output=True)
    r = make_daemon(s, output_off_on_exit=True)
    r.stop()
    assert s.output is False
    assert s.setters_received() == ["OUTP OFF"]


def test_status_document(make_daemon):
    s = SimulatedSupply(output=True, voltage=13.8, current=4.0, load_ohms=10.0)
    r = make_daemon(s)
    assert wait_for(lambda: r.d.status()["setpoints"]["voltage"] == 13.8)
    code, st = r.req("GET", "/api/status")
    assert code == 200
    assert st["output"] is True and st["mode"] == "cv" and st["mode_code"] == 1
    assert st["measured"]["current"] == 1.38
    assert st["setpoints"] == {"voltage": 13.8, "current": 4.0}
    assert st["protection"]["ovp_limit"] == 63.0 and st["protection"]["ocp_tripped"] is False
    assert st["identity"]["model"] == "DC620S"
    assert st["link"]["ok"] is True and st["link"]["age_s"] < 1
    assert st["ceilings"] == {"max_voltage": 15.0, "max_current": 10.0}
    assert st["daemon"]["writes_enabled"] is True


def test_status_page_and_health(make_daemon):
    r = make_daemon()
    code, body = r.req("GET", "/")
    assert code == 200 and b"Bench PSU" in body
    assert r.req("GET", "/api/health")[0] == 200


def test_front_panel_change_is_noticed_and_logged(make_daemon, capsys):
    s = SimulatedSupply()
    r = make_daemon(s)
    s.voltage = 5.0
    s.output = True
    assert wait_for(lambda: r.d.status()["setpoints"]["voltage"] == 5.0)
    assert wait_for(lambda: r.d.status()["output"] is True)
    out = capsys.readouterr().out
    assert '"event":"setpoints"' in out and '"event":"output"' in out


def test_write_with_token(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    code, body = r.req("POST", "/api/set", {"volts": 13.8, "amps": 4}, token=TOKEN)
    assert code == 200, body
    assert body["result"]["voltage"] == 13.8 and body["status"]["setpoints"]["current"] == 4.0


def test_ceiling_via_http_sends_nothing(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    code, body = r.req("POST", "/api/voltage", {"volts": 30}, token=TOKEN)
    assert code == 422 and body["kind"] == "limit"
    assert s.setters_received() == []


def test_link_lost_and_reconnect(make_daemon):
    s = SimulatedSupply(output=True)
    r = make_daemon(s)
    s.drop_replies = 6
    assert wait_for(lambda: r.d.status()["link"]["state"] != "ok")
    assert wait_for(lambda: r.d.status()["link"]["state"] == "ok", 5)
    assert r.d.status()["link"]["reconnects"] >= 2
    assert s.output is True and s.setters_received() == []


def test_write_while_disconnected_is_503(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    r.d.port_factory = lambda: (_ for _ in ()).throw(__import__("kiprim.transport").transport.TransportError("gone"))
    s.drop_replies = 1000
    assert wait_for(lambda: r.d.status()["link"]["state"] == "absent", 5)
    code, body = r.req("POST", "/api/output", {"on": True}, token=TOKEN)
    assert code == 503
