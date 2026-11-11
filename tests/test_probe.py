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
