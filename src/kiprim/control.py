# SPDX-License-Identifier: Apache-2.0
"""Safe write operations on top of :class:`kiprim.instrument.Instrument`.

Rules enforced here, before anything is sent:

* setpoints never exceed the host ceilings (``max_voltage``/``max_current``)
  or the hardware range;
* a voltage setpoint never exceeds the present OVP limit and a current
  setpoint never exceeds the present OCP limit, and a limit is never set
  below the present setpoint (refused rather than reordered, so nothing the
  caller did not ask for is changed);
* a current change while the output is on goes output OFF -> CURR -> output
  ON (the "cycle" order), because several drivers for this firmware family
  report that a live ``CURR`` write can trip OCP. ``live=True`` skips the
  cycle, on explicit request only. If the ``CURR`` write fails mid-cycle the
  output is left OFF and the error says so: turning a load back on with an
  unknown current limit is worse than leaving it off.

Nothing in this module changes the output state except :meth:`set_output`
and the explicit cycle above.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import instrument as ins
from .instrument import Instrument


class LimitError(ValueError):
    """A request would exceed a ceiling or the hardware range (nothing sent)."""


class ConflictError(ValueError):
    """A request conflicts with the supply's present setpoints (nothing sent)."""


class CycleError(RuntimeError):
    """A current change failed while the output was cycled off; output is OFF."""


@dataclass
class Ceilings:
    max_voltage: float = 15.0
    max_current: float = 10.0

    def __post_init__(self):
        if not 0 < self.max_voltage <= ins.DEVICE_MAX_VOLTAGE:
            raise ValueError(f"max_voltage must be in (0, {ins.DEVICE_MAX_VOLTAGE}]")
        if not 0 < self.max_current <= ins.DEVICE_MAX_CURRENT:
            raise ValueError(f"max_current must be in (0, {ins.DEVICE_MAX_CURRENT}]")


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitError(f"{name} must be a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise LimitError(f"{name} must be finite")
    if value < 0:
        raise LimitError(f"{name} must not be negative")
    return value


class Controller:
    def __init__(self, inst: Instrument, ceilings: Ceilings):
        self.inst = inst
        self.ceilings = ceilings

    # ---- validation (no I/O) ------------------------------------------
    def check_voltage(self, volts) -> float:
        v = _number(volts, "voltage")
        if v > self.ceilings.max_voltage:
            raise LimitError(f"{v} V is above the host ceiling of {self.ceilings.max_voltage} V")
        return ins.quantize(v, ins.VOLT_STEP)

    def check_current(self, amps) -> float:
        a = _number(amps, "current")
        if a > self.ceilings.max_current:
            raise LimitError(f"{a} A is above the host ceiling of {self.ceilings.max_current} A")
        return ins.quantize(a, ins.CURR_STEP)

    @staticmethod
    def check_ovp(volts) -> float:
        v = _number(volts, "OVP")
        if v > ins.DEVICE_MAX_OVP:
            raise LimitError(f"OVP {v} V is above the hardware range ({ins.DEVICE_MAX_OVP} V)")
        return ins.quantize(v, ins.VOLT_STEP)

    @staticmethod
    def check_ocp(amps) -> float:
        a = _number(amps, "OCP")
        if a > ins.DEVICE_MAX_OCP:
            raise LimitError(f"OCP {a} A is above the hardware range ({ins.DEVICE_MAX_OCP} A)")
        return ins.quantize(a, ins.CURR_STEP)

    # ---- operations ----------------------------------------------------
    def set_voltage(self, volts) -> dict:
        v = self.check_voltage(volts)
        ovp = self.inst.ovp_limit()
        if v > ovp:
            raise ConflictError(f"{v} V is above the OVP limit {ovp} V; raise OVP first")
        got = self.inst.set_voltage(v)
        return {"voltage": got}

    def set_current(self, amps, live: bool = False) -> dict:
        a = self.check_current(amps)
        ocp = self.inst.ocp_limit()
        if a > ocp:
            raise ConflictError(f"{a} A is above the OCP limit {ocp} A; raise OCP first")
        before = self.inst.current_setpoint()
        if abs(before - a) <= ins.CURR_STEP / 2:
            return {"current": before, "changed": False, "cycled": False}
        out = self.inst.output()
        if out and not live:
            self.inst.set_output(False)
            try:
                got = self.inst.set_current(a)
            except Exception as exc:
                raise CycleError(f"current change failed with the output cycled off; "
                                 f"output left OFF: {exc}") from exc
            self.inst.set_output(True)
            return {"current": got, "changed": True, "cycled": True}
        got = self.inst.set_current(a)
        return {"current": got, "changed": True, "cycled": False}

    def set_both(self, volts, amps, live: bool = False) -> dict:
        """Set voltage and current; at most one output cycle, only if the
        current actually changes while the output is on."""
        v = self.check_voltage(volts)
        a = self.check_current(amps)
        ovp, ocp = self.inst.ovp_limit(), self.inst.ocp_limit()
        if v > ovp:
            raise ConflictError(f"{v} V is above the OVP limit {ovp} V; raise OVP first")
        if a > ocp:
            raise ConflictError(f"{a} A is above the OCP limit {ocp} A; raise OCP first")
        current_changes = abs(self.inst.current_setpoint() - a) > ins.CURR_STEP / 2
        out = self.inst.output()
        if current_changes and out and not live:
            self.inst.set_output(False)
            try:
                gv = self.inst.set_voltage(v)
                ga = self.inst.set_current(a)
            except Exception as exc:
                raise CycleError(f"setpoint change failed with the output cycled off; "
                                 f"output left OFF: {exc}") from exc
            self.inst.set_output(True)
            return {"voltage": gv, "current": ga, "cycled": True}
        gv = self.inst.set_voltage(v)
        ga = self.inst.set_current(a) if current_changes else self.inst.current_setpoint()
        return {"voltage": gv, "current": ga, "cycled": False}

    def set_output(self, on: bool) -> dict:
        if not isinstance(on, bool):
            raise LimitError("output must be true or false")
        if on:
            # Refuse to switch on with setpoints above the host ceilings (they
            # may have been set from the front panel).
            sp = self.inst.setpoints()
            if sp.voltage > self.ceilings.max_voltage + ins.VOLT_STEP / 2:
                raise ConflictError(
                    f"voltage setpoint {sp.voltage} V is above the host ceiling "
                    f"{self.ceilings.max_voltage} V; lower it before switching on")
            if sp.current > self.ceilings.max_current + ins.CURR_STEP / 2:
                raise ConflictError(
                    f"current setpoint {sp.current} A is above the host ceiling "
                    f"{self.ceilings.max_current} A; lower it before switching on")
        return {"output": self.inst.set_output(on)}

    def set_ovp(self, volts) -> dict:
        v = self.check_ovp(volts)
        sp = self.inst.voltage_setpoint()
        if v < sp:
            raise ConflictError(f"OVP {v} V is below the voltage setpoint {sp} V; lower the setpoint first")
        return {"ovp": self.inst.set_ovp(v)}

    def set_ocp(self, amps) -> dict:
        a = self.check_ocp(amps)
        sp = self.inst.current_setpoint()
        if a < sp:
            raise ConflictError(f"OCP {a} A is below the current setpoint {sp} A; lower the setpoint first")
        return {"ocp": self.inst.set_ocp(a)}

    def set_lock(self, locked: bool) -> dict:
        if not isinstance(locked, bool):
            raise LimitError("locked must be true or false")
        got = self.inst.set_lock(locked)
        return {"locked": locked if got is None else got, "verified": got is not None}
