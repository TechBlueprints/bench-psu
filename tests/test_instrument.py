# SPDX-License-Identifier: Apache-2.0
import pytest

from kiprim.instrument import Instrument, ReadbackMismatch
from kiprim.simulator import SimPort, SimulatedSupply
from kiprim.transport import DeviceError, Transport


def make(**switches):
    s = SimulatedSupply(**switches)
    return s, Instrument(Transport(SimPort(s), 0.05, 0.01))


def test_queries_match_real_unit(inst):
    assert inst.idn().model == "DC620S"
    assert inst.output() is False
    sp = inst.setpoints()
    assert (sp.voltage, sp.current, sp.ovp, sp.ocp) == (10.0, 2.0, 63.0, 21.0)
    m = inst.measure_all()
    assert m.power is None  # two fields, like V2.3.0
    assert inst.measure_info().mode == "standby"


def test_setters_read_back(inst, supply):
    assert inst.set_voltage(13.8) == 13.8
    assert inst.set_current(4) == 4.0
    assert supply.log[-2:] == ["CURR 4.000", "CURR?"]
    assert inst.set_ovp(15) == 15.0 and inst.set_ocp(5) == 5.0
    assert inst.set_output(True) is True
    assert inst.measure_info().mode == "cv"


def test_voltage_quantized_to_10mv(inst, supply):
    inst.set_voltage(13.804)
    assert "VOLT 13.80" in supply.log


def test_readback_mismatch_raises():
    s, inst = make(readback_offset=0.5)
    with pytest.raises(ReadbackMismatch):
        inst.set_voltage(12)


def test_device_rejects_out_of_range(inst):
    with pytest.raises(DeviceError):
        inst.set_voltage(70)


@pytest.mark.parametrize("switches", [
    {"meas_all_fields": 3}, {"separator": " "}, {"outp_digits": True},
    {"meas_all_fields": 3, "separator": " ", "outp_digits": True},
])
def test_formats_tolerated(switches):
    s, inst = make(**switches)
    inst.set_output(True)
    assert inst.output() is True
    m = inst.measure_all()
    assert m.voltage == 10.0
    assert (m.power is not None) == (switches.get("meas_all_fields") == 3)


def test_info_unsupported_falls_back():
    s, inst = make(info_supported=False)
    assert inst.measure_info() is None
    assert inst.has_info is False
    n = len(s.log)
    assert inst.measure_info() is None and len(s.log) == n  # not asked again


def test_lock_without_query_is_unverified():
    s, inst = make()
    assert inst.set_lock(True) is None
    assert s.locked


def test_lock_with_query_verifies():
    s, inst = make(lock_query=True)
    assert inst.set_lock(True) is True
    assert inst.set_lock(False) is False
