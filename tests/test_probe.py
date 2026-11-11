# SPDX-License-Identifier: Apache-2.0
import pytest

from conftest import TOKEN

from kiprim import probe
from kiprim.control import Ceilings
from kiprim.simulator import SimulatedSupply


def by_cmd(rows):
    return {r.command if hasattr(r, "command") else r["command"]: r for r in rows}


def test_queries_only_changes_nothing(inst, supply):
    supply.output = True
    rows = by_cmd(probe.run(inst, "queries"))
    assert supply.setters_received() == []
    assert rows["SYST:STAT?"].result == "err"
    assert rows["MEAS:ALL?"].note == "2 fields"
    assert rows["MEAS:ALL:INFO?"].note == "mode 1 = cv"
    assert rows["*RST"].result == "skipped"
    assert "SYST:LOCK?" not in rows


def test_setters_need_output_off(inst, supply):
    supply.output = True
    with pytest.raises(probe.ProbeRefused):
        probe.run(inst, "setters")
    assert supply.setters_received() == []


def test_setters_are_identity_writes(inst, supply):
    supply.voltage, supply.current = 13.8, 4.0
    rows = probe.run(inst, "setters")
    assert (supply.voltage, supply.current, supply.ovp, supply.ocp) == (13.8, 4.0, 63.0, 21.0)
    assert supply.output is False and supply.locked is False
    assert all(r.result in ("ok", "err", "info", "skipped") for r in rows), [r for r in rows if r.result == "mismatch"]
    assert "OUTP ON" not in supply.setters_received()


def test_ocp_quirk_level_detects_quirk():
    from kiprim.instrument import Instrument
    from kiprim.simulator import SimPort
    from kiprim.transport import Transport
    for quirk, words in ((True, "quirk reproduced"), (False, "NOT reproduced")):
        s = SimulatedSupply(ocp_quirk=quirk)
        rows = by_cmd(probe.run(Instrument(Transport(SimPort(s), 0.05, 0.01)), "ocp_quirk",
                                Ceilings(15, 10)))
        assert words in rows["(live CURR quirk)"].note
        assert s.output is False


def test_markdown():
    md = probe.markdown([{"command": "OUTP?", "kind": "query", "reply": "ON", "result": "ok",
                          "note": "", "ms": 7.5}])
    assert "| `OUTP?` | query | `ON` | ok |  | 7.5 |" in md


def test_probe_via_daemon(make_daemon):
    r = make_daemon(SimulatedSupply(output=True))
    code, body = r.req("POST", "/api/probe", {"level": "queries"}, token=TOKEN)
    assert code == 200 and len(body["rows"]) >= 12
    code, body = r.req("POST", "/api/probe", {"level": "setters"}, token=TOKEN)
    assert code == 409
    assert r.supply.setters_received() == []
    assert r.req("POST", "/api/probe", {"level": "queries"})[0] == 401


def test_output_levels_respect_ceilings(inst, supply):
    supply.voltage = 24.0
    for ceil in (None, Ceilings(15, 10)):
        with pytest.raises(probe.ProbeRefused):
            probe.run(inst, "output", ceil)
    assert "OUTP ON" not in supply.setters_received()


# ---- exception safety (review 2026-11-11) ---------------------------------------
from kiprim.instrument import Instrument  # noqa: E402
from kiprim.simulator import SimPort  # noqa: E402
from kiprim.transport import Transport  # noqa: E402


def _inst(s):
    return Instrument(Transport(SimPort(s), 0.05, 0.01))


def test_error_after_lock_on_unlocks_again():
    s = SimulatedSupply(lock_query=True)
    real = s._dispatch
    state = {"n": 0}

    def flaky(cmd):
        if cmd == "SYST:LOCK?":
            state["n"] += 1
            if state["n"] == 1:
                return "LOCKED?"  # unparseable -> recorded as bad-format, not fatal
        return real(cmd)
    s._dispatch = flaky
    rows = probe.run(_inst(s), "setters")
    assert s.locked is False
    assert any(r.result == "bad-format" for r in rows)


def test_exception_mid_lock_still_unlocks():
    s = SimulatedSupply(lock_query=True)
    inst = _inst(s)
    original = probe._setter_row

    def boom(inst_, command, readback, expect):
        if command == "SYST:LOCK OFF" and not getattr(boom, "fired", False):
            boom.fired = True
            raise RuntimeError("simulated failure right after SYST:LOCK ON")
        return original(inst_, command, readback, expect)
    probe._setter_row = boom
    try:
        with pytest.raises(probe.ProbeAborted) as e:
            probe.run(inst, "setters")
    finally:
        probe._setter_row = original
    assert s.locked is False
    assert any("cleanup" in r.note for r in e.value.rows)


def test_exception_after_output_on_switches_it_off():
    s = SimulatedSupply()
    inst = _inst(s)
    original = inst.measure_info
    inst.measure_info = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    original_query_row = probe._query_row

    def qrow(inst_, command, parser):
        if command == "MEAS:ALL:INFO?" and s.output:
            raise RuntimeError("simulated failure with the output on")
        return original_query_row(inst_, command, parser)
    probe._query_row = qrow
    try:
        with pytest.raises(probe.ProbeAborted):
            probe.run(inst, "output", Ceilings(15, 10))
    finally:
        probe._query_row = original_query_row
        inst.measure_info = original
    assert s.output is False
    assert s.setters_received()[-1] == "OUTP OFF"


def test_ocp_quirk_level_also_writes_ocp_live():
    s = SimulatedSupply()
    rows = by_cmd(probe.run(_inst(s), "ocp_quirk", Ceilings(15, 10)))
    assert "(live CURR quirk)" in rows and "(live CURR:LIM quirk)" in rows
    assert s.output is False


def test_output_levels_refuse_within_margin():
    s = SimulatedSupply()
    s.voltage, s.ovp = 13.0, 13.05
    with pytest.raises(probe.ProbeRefused, match="margin"):
        probe.run(_inst(s), "output", Ceilings(15, 10))
    assert "OUTP ON" not in s.setters_received()
