# SPDX-License-Identifier: Apache-2.0
import pytest

from kiprim.control import Ceilings, ConflictError, Controller, CycleError, LimitError
from kiprim.instrument import Instrument
from kiprim.simulator import SimPort, SimulatedSupply
from kiprim.transport import Transport


def make(ceil=(15.0, 10.0), **switches):
    s = SimulatedSupply(**switches)
    inst = Instrument(Transport(SimPort(s), 0.05, 0.01))
    return s, Controller(inst, Ceilings(*ceil))


@pytest.mark.parametrize("call", [
    lambda c: c.set_voltage(15.01),
    lambda c: c.set_current(10.5),
    lambda c: c.set_both(16, 1),
    lambda c: c.set_both(12, 11),
    lambda c: c.set_voltage(-1),
    lambda c: c.set_voltage(float("nan")),
    lambda c: c.set_voltage("12"),
    lambda c: c.set_voltage(True),
    lambda c: c.set_ovp(64),
    lambda c: c.set_ocp(22),
])
def test_ceilings_enforced_before_anything_is_sent(call):
    s, c = make()
    with pytest.raises(LimitError):
        call(c)
    assert s.log == []


def test_ceiling_config_validated():
    with pytest.raises(ValueError):
        Ceilings(61, 5)
    with pytest.raises(ValueError):
        Ceilings(15, 0)


def test_voltage_above_ovp_refused():
    s, c = make()
    s.ovp = 12.0
    with pytest.raises(ConflictError):
        c.set_voltage(13)
    assert s.setters_received() == []


def test_ovp_below_setpoint_refused():
    s, c = make()
    s.voltage = 13.8
    with pytest.raises(ConflictError):
        c.set_ovp(13)
    assert c.set_ovp(14.5) == {"ovp": 14.5, "cycled": False}


def test_current_change_cycles_output_when_on():
    s, c = make(ocp_quirk=True)
    s.output = True
    r = c.set_current(4)
    assert r == {"current": 4.0, "changed": True, "cycled": True}
    assert s.setters_received() == ["OUTP OFF", "CURR 4.000", "OUTP ON"]
    assert s.output and not s.ocp_tripped


def test_current_change_no_cycle_when_off():
    s, c = make()
    c.set_current(4)
    assert s.setters_received() == ["CURR 4.000"]


def test_live_current_change_only_when_asked():
    s, c = make(ocp_quirk=True)
    s.output = True
    r = c.set_current(4, live=True)
    assert r["cycled"] is False
    assert s.setters_received() == ["CURR 4.000"]
    assert s.ocp_tripped and not s.output  # the quirk, modelled


def test_unchanged_current_is_noop():
    s, c = make()
    s.output = True
    assert c.set_current(2.0)["changed"] is False
    assert s.setters_received() == []


def test_failed_current_mid_cycle_leaves_output_off():
    s, c = make()
    s.output = True
    s.readback_offset = 0.0
    original = c.inst.set_current

    def broken(a):
        s.readback_offset = 0.5
        try:
            return original(a)
        finally:
            s.readback_offset = 0.0
    c.inst.set_current = broken
    with pytest.raises(CycleError, match="output left OFF"):
        c.set_current(4)
    assert s.output is False
    assert "OUTP ON" not in s.setters_received()


def test_set_both_single_cycle():
    s, c = make()
    s.output = True
    r = c.set_both(13.8, 4)
    assert r["cycled"] is True
    assert s.setters_received() == ["OUTP OFF", "VOLT 13.80", "CURR 4.000", "OUTP ON"]


def test_set_both_voltage_only_is_live():
    s, c = make()
    s.output = True
    r = c.set_both(12.5, 2.0)
    assert r["cycled"] is False
    assert s.setters_received() == ["VOLT 12.50"]


def test_output_on_refused_above_ceiling():
    s, c = make(ceil=(5.0, 1.0))
    with pytest.raises(ConflictError):
        c.set_output(True)  # front panel left 10 V / 2 A
    assert s.setters_received() == []
    assert c.set_output(False) == {"output": False}


def test_output_on_and_off():
    s, c = make()
    assert c.set_output(True) == {"output": True}
    assert c.set_output(False) == {"output": False}


# ---- margins and the corrected quirk handling (review 2026-11-11) --------------
def test_ocp_equal_to_flowing_current_is_refused():
    # The reported trip: OCP written at the level the load is drawing.
    s, c = make()
    s.voltage, s.current, s.output, s.load_ohms = 12.0, 4.0, True, 1.0  # CC at 4 A
    with pytest.raises(ConflictError, match="less than 0.1 A above"):
        c.set_ocp(4.0)
    assert s.setters_received() == []
    assert c.set_ocp(4.1)["ocp"] == 4.1


def test_current_needs_ocp_margin():
    s, c = make()
    s.ocp = 5.0
    with pytest.raises(ConflictError, match="needs OCP at least 5.0"):
        c.set_current(4.95)
    assert s.setters_received() == []
    assert c.set_current(4.9)["current"] == 4.9


def test_voltage_needs_ovp_margin():
    s, c = make()
    s.ovp = 14.0
    with pytest.raises(ConflictError, match="needs OVP"):
        c.set_voltage(13.95)
    assert c.set_voltage(13.9) == {"voltage": 13.9}


def test_ocp_write_cycles_output_when_on():
    s, c = make()
    s.output = True
    r = c.set_ocp(5.0)
    assert r == {"ocp": 5.0, "cycled": True}
    assert s.setters_received() == ["OUTP OFF", "CURR:LIM 5.000", "OUTP ON"]


def test_ovp_write_cycles_output_when_on_unless_live():
    s, c = make()
    s.output = True
    assert c.set_ovp(15.0, live=True) == {"ovp": 15.0, "cycled": False}
    assert s.setters_received() == ["VOLT:LIM 15.00"]
    assert c.set_ovp(16.0)["cycled"] is True


def test_live_must_be_a_real_boolean():
    s, c = make()
    s.output = True
    for bad in ("false", "true", 1, None):
        with pytest.raises(LimitError):
            c.set_current(4, live=bad)
    assert s.setters_received() == []


def test_output_on_refused_within_margin():
    s, c = make()
    s.voltage, s.ovp = 13.0, 13.05  # front panel left OVP almost at the setpoint
    with pytest.raises(ConflictError, match="needs OVP"):
        c.set_output(True)
    assert s.setters_received() == []


def test_margins_configurable_and_validated():
    s, c = make()
    c.ceilings = Ceilings(15.0, 10.0, ovp_margin=0.0, ocp_margin=0.0)
    s.ocp = 4.0
    assert c.set_current(4.0)["current"] == 4.0
    with pytest.raises(ValueError):
        Ceilings(15, 10, ocp_margin=-1)
