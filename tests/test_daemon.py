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
    assert st["ceilings"] == {"max_voltage": 15.0, "max_current": 10.0,
                              "ovp_margin": 0.1, "ocp_margin": 0.1}
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
    assert r.d.status()["link"]["reconnects"] >= 1  # the first connect does not count
    assert s.output is True and s.setters_received() == []


def test_write_while_disconnected_is_503(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    r.d.port_factory = lambda: (_ for _ in ()).throw(__import__("kiprim.transport").transport.TransportError("gone"))
    s.drop_replies = 1000
    assert wait_for(lambda: r.d.status()["link"]["state"] == "absent", 5)
    code, body = r.req("POST", "/api/output", {"on": True}, token=TOKEN)
    assert code == 503


# ---- review fixes (2026-11-11) -----------------------------------------------------
import json  # noqa: E402

import pytest  # noqa: E402
import threading  # noqa: E402

from kiprim import daemon as daemon_mod  # noqa: E402


def test_worker_survives_unexpected_error_on_reconnect(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s)
    calls = {"n": 0}
    from kiprim.simulator import SimPort

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("tcgetattr failed")  # not a TransportError
        return SimPort(s)
    r.d.port_factory = factory
    s.drop_replies = 3  # -> link lost -> reconnect through the failing factory
    assert wait_for(lambda: calls["n"] >= 2 and r.d.status()["link"]["state"] == "ok", 5)
    assert r.d.worker.is_alive()


def test_worker_survives_unexpected_error_in_poll(make_daemon, capsys):
    s = SimulatedSupply(output=True)
    r = make_daemon(s)
    real = r.d._refresh
    fired = {"n": 0}

    def bad(full):
        if fired["n"] == 0:
            fired["n"] += 1
            raise RuntimeError("bug in the poll path")
        return real(full)
    r.d._refresh = bad
    assert wait_for(lambda: fired["n"] == 1)
    assert wait_for(lambda: r.d.status()["link"]["state"] == "ok", 5)
    assert r.d.worker.is_alive()
    assert '"event":"worker_error"' in capsys.readouterr().out
    assert s.output is True and s.setters_received() == []


def test_health_goes_red_when_polls_stop(make_daemon):
    r = make_daemon(stale_after=0.5)
    gate = threading.Event()
    r.d._refresh = lambda full: gate.wait(5)  # a hung poll
    time.sleep(1.2)
    code, body = r.req("GET", "/api/health")
    st = r.d.status()
    gate.set()
    assert code == 503 and body["link"] == "stale"
    assert any("stale" in w for w in st["warnings"])


def test_worker_dead_is_reported():
    from kiprim.config import Config
    d = daemon_mod.Daemon(Config())
    d.worker = threading.Thread(target=lambda: None)
    d.worker.start()
    d.worker.join()
    assert d.status()["link"]["state"] == "worker-dead"


def test_wrong_device_gets_only_idn(make_daemon):
    s = SimulatedSupply()
    s.model = "DC310S"
    from kiprim.config import Config
    from kiprim.simulator import SimPort
    cfg = Config(device="sim", reply_timeout=0.05, setter_error_window=0.01, reconnect_interval=0.05)
    d = daemon_mod.Daemon(cfg, lambda: SimPort(s))
    d.start()
    assert wait_for(lambda: d.status()["link"]["state"] == "wrong-device")
    time.sleep(0.2)
    d.stop()
    assert set(s.log) == {"*IDN?"}


def test_serial_pin(make_daemon):
    s = SimulatedSupply()
    r = make_daemon(s, expect_serial="SIM00001")
    assert r.d.status()["identity"]["serial"] == "SIM00001"
    from kiprim.config import Config
    from kiprim.simulator import SimPort
    d = daemon_mod.Daemon(Config(reply_timeout=0.05, reconnect_interval=0.05, expect_serial="OTHER"),
                          lambda: SimPort(SimulatedSupply()))
    d.start()
    assert wait_for(lambda: d.status()["link"]["state"] == "wrong-device")
    d.stop()


def test_energy_counters_and_reset(make_daemon):
    s = SimulatedSupply(output=True, voltage=10.0, current=5.0, load_ohms=10.0)  # 1 A, 10 W
    r = make_daemon(s)
    time.sleep(1.0)
    e = r.d.status()["energy"]
    assert 0.4 < e["output_on_s"] < 2.0
    assert abs(e["wh"] - 10 * e["output_on_s"] / 3600) < 1e-3
    assert abs(e["ah"] - 1 * e["output_on_s"] / 3600) < 1e-4
    code, body = r.req("POST", "/api/counters/reset", {}, token=TOKEN)
    assert code == 200 and body["result"]["energy"]["wh"] == 0.0
    assert r.req("POST", "/api/counters/reset", {})[0] == 401


def test_samples_logged(make_daemon, capsys):
    s = SimulatedSupply(output=True)
    make_daemon(s, sample_interval_on=0.2)
    time.sleep(0.7)
    samples = [json.loads(l) for l in capsys.readouterr().out.splitlines() if '"event":"sample"' in l]
    assert len(samples) >= 2
    assert {"voltage", "current", "power", "output", "mode", "wh", "ah"} <= samples[0].keys()


def test_poll_rate_is_fixed(make_daemon):
    r = make_daemon(poll_interval=0.05)
    r.d._poll_window = (time.monotonic(), 0)
    time.sleep(5.3)
    assert 15 <= r.d.poll_hz <= 21  # 20 Hz target; the old scheduling drifted low


def test_log_lines_never_interleave(capsys):
    def spam(n):
        for i in range(200):
            daemon_mod.log_event("t", n=n, i=i, pad="x" * 200)
    threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [l for l in capsys.readouterr().out.splitlines() if l]
    assert len(lines) == 1600
    for l in lines:
        json.loads(l)


def test_job_timeout_drops_unsent_job():
    from kiprim.config import Config
    d = daemon_mod.Daemon(Config())
    sent = []
    with pytest.raises(TimeoutError, match="dropped before it was sent"):
        d.submit("set_output", lambda: sent.append(1), timeout=0.05)  # no worker running
    job = d.jobs.get_nowait()
    d._run_job(job)  # the worker picks it up late: it must not run
    assert job.cancelled and sent == []
