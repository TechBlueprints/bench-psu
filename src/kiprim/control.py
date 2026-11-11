# SPDX-License-Identifier: Apache-2.0
"""Safe write operations on top of :class:`kiprim.instrument.Instrument`.

Rules enforced here, before anything is sent:

* setpoints never exceed the host ceilings (``max_voltage``/``max_current``)
  or the hardware range;
* the protection limits keep a margin above the setpoints: voltage setpoint
  <= OVP - ``ovp_margin``, current setpoint <= OCP - ``ocp_margin``. A
  request that would break that is refused, not reordered, so nothing the
  caller did not ask for is changed;
* with the output on, a change to the current setpoint or to either
  protection limit goes output OFF -> write -> output ON (the "cycle").
  Why: the drivers that run this firmware family (Wajalot/lab_Kiprim and
  kibarra-esp32) report that OCP and the ``CURR:LIM`` value are one register
  and that writing it with the output on and current flowing near the new
  value tripped OCP on the bench. On the DC620S V2.3.0, ``CURR`` and
  ``CURR:LIM`` read back separately and a live ``CURR`` write with open
  terminals did not move ``CURR:LIM``, but no live write has been tested
  with current flowing, so the cycle stays the default. ``live=True`` skips
  it, on explicit request only;
* if a write fails mid-cycle, the output is left OFF and the error says so:
  switching a load back on with an unknown limit is worse than leaving it off.

Voltage setpoint changes are applied live (no report of a problem, and
verified live on the real unit).

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
    """A request conflicts with the supply's present setpoints (no setter sent)."""


class CycleError(RuntimeError):
    """A write failed while the output was cycled off; the output is OFF."""


@dataclass
class Ceilings:
    max_voltage: float = 15.0
    max_current: float = 10.0
    ovp_margin: float = 0.1  # V: OVP must sit at least this far above the voltage setpoint
    ocp_margin: float = 0.1  # A: OCP must sit at least this far above the current setpoint

    def __post_init__(self):
        if not 0 < self.max_voltage <= ins.DEVICE_MAX_VOLTAGE:
            raise ValueError(f"max_voltage must be in (0, {ins.DEVICE_MAX_VOLTAGE}]")
        if not 0 < self.max_current <= ins.DEVICE_MAX_CURRENT:
            raise ValueError(f"max_current must be in (0, {ins.DEVICE_MAX_CURRENT}]")
        if not 0 <= self.ovp_margin <= 5:
            raise ValueError("ovp_margin must be in [0, 5] V")
        if not 0 <= self.ocp_margin <= 5:
            raise ValueError("ocp_margin must be in [0, 5] A")


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitError(f"{name} must be a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise LimitError(f"{name} must be finite")
    if value < 0:
        raise LimitError(f"{name} must not be negative")
    return value


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise LimitError(f"{name} must be true or false")
    return value


EPS = 1e-9


class Controller:
    def __init__(self, inst: Instrument, ceilings: Ceilings):
        self.inst = inst
        self.ceilings = ceilings

    # ---- validation (no I/O) ------------------------------------------
    def check_voltage(self, volts) -> float:
        v = _number(volts, "voltage")
        if v > self.ceilings.max_voltage + EPS:
            raise LimitError(f"{v} V is above the host ceiling of {self.ceilings.max_voltage} V")
        return ins.quantize(v, ins.VOLT_STEP)

    def check_current(self, amps) -> float:
        a = _number(amps, "current")
        if a > self.ceilings.max_current + EPS:
            raise LimitError(f"{a} A is above the host ceiling of {self.ceilings.max_current} A")
        return ins.quantize(a, ins.CURR_STEP)

    @staticmethod
    def check_ovp(volts) -> float:
        v = _number(volts, "OVP")
        if v > ins.DEVICE_MAX_OVP + EPS:
            raise LimitError(f"OVP {v} V is above the hardware range ({ins.DEVICE_MAX_OVP} V)")
        return ins.quantize(v, ins.VOLT_STEP)

    @staticmethod
    def check_ocp(amps) -> float:
        a = _number(amps, "OCP")
        if a > ins.DEVICE_MAX_OCP + EPS:
            raise LimitError(f"OCP {a} A is above the hardware range ({ins.DEVICE_MAX_OCP} A)")
        return ins.quantize(a, ins.CURR_STEP)

    def _voltage_fits(self, v: float, ovp: float) -> None:
        m = self.ceilings.ovp_margin
        if v > ovp - m + EPS:
            raise ConflictError(f"{v} V needs OVP at least {ins.quantize(v + m, ins.VOLT_STEP)} V "
                                f"(margin {m} V); OVP is {ovp} V, raise it first")

    def _current_fits(self, a: float, ocp: float) -> None:
        m = self.ceilings.ocp_margin
        if a > ocp - m + EPS:
            raise ConflictError(f"{a} A needs OCP at least {ins.quantize(a + m, ins.CURR_STEP)} A "
                                f"(margin {m} A); OCP is {ocp} A, raise it first")

    def _cycled(self, writes, live: bool) -> bool:
        """Run ``writes()``; with the output on and not ``live``, inside an
        OFF -> writes -> ON cycle. Returns whether the output was cycled."""
        if live or not self.inst.output():
            writes()
            return False
        self.inst.set_output(False)
        try:
            writes()
        except Exception as exc:
            raise CycleError(f"write failed with the output cycled off; output left OFF: {exc}") from exc
        self.inst.set_output(True)
        return True

    # ---- operations ----------------------------------------------------
    def set_voltage(self, volts) -> dict:
        v = self.check_voltage(volts)
        self._voltage_fits(v, self.inst.ovp_limit())
        return {"voltage": self.inst.set_voltage(v)}

    def set_current(self, amps, live: bool = False) -> dict:
        a = self.check_current(amps)
        live = _bool(live, "live")
        self._current_fits(a, self.inst.ocp_limit())
        before = self.inst.current_setpoint()
        if abs(before - a) <= ins.CURR_STEP / 2:
            return {"current": before, "changed": False, "cycled": False}
        got = {}
        cycled = self._cycled(lambda: got.update(current=self.inst.set_current(a)), live)
        return {"current": got["current"], "changed": True, "cycled": cycled}

    def set_both(self, volts, amps, live: bool = False) -> dict:
        """Set voltage and current; at most one output cycle, and only if the
        current actually changes while the output is on."""
        v = self.check_voltage(volts)
        a = self.check_current(amps)
        live = _bool(live, "live")
        self._voltage_fits(v, self.inst.ovp_limit())
        self._current_fits(a, self.inst.ocp_limit())
        if abs(self.inst.current_setpoint() - a) <= ins.CURR_STEP / 2:
            return {"voltage": self.inst.set_voltage(v), "current": self.inst.current_setpoint(),
                    "cycled": False}
        got = {}

        def writes():
            got["voltage"] = self.inst.set_voltage(v)
            got["current"] = self.inst.set_current(a)
        cycled = self._cycled(writes, live)
        return {**got, "cycled": cycled}

    def set_output(self, on) -> dict:
        on = _bool(on, "output")
        if on:
            # Refuse to switch on with setpoints above the host ceilings or
            # too close to the protection limits (front-panel changes).
            sp = self.inst.setpoints()
            if sp.voltage > self.ceilings.max_voltage + ins.VOLT_STEP / 2:
                raise ConflictError(
                    f"voltage setpoint {sp.voltage} V is above the host ceiling "
                    f"{self.ceilings.max_voltage} V; lower it before switching on")
            if sp.current > self.ceilings.max_current + ins.CURR_STEP / 2:
                raise ConflictError(
                    f"current setpoint {sp.current} A is above the host ceiling "
                    f"{self.ceilings.max_current} A; lower it before switching on")
            self._voltage_fits(sp.voltage, sp.ovp)
            self._current_fits(sp.current, sp.ocp)
        return {"output": self.inst.set_output(on)}

    def set_ovp(self, volts, live: bool = False) -> dict:
        v = self.check_ovp(volts)
        live = _bool(live, "live")
        sp = self.inst.voltage_setpoint()
        m = self.ceilings.ovp_margin
        if v < sp + m - EPS:
            raise ConflictError(f"OVP {v} V is less than {m} V above the voltage setpoint {sp} V; "
                                "lower the setpoint first")
        got = {}
        cycled = self._cycled(lambda: got.update(ovp=self.inst.set_ovp(v)), live)
        return {**got, "cycled": cycled}

    def set_ocp(self, amps, live: bool = False) -> dict:
        a = self.check_ocp(amps)
        live = _bool(live, "live")
        sp = self.inst.current_setpoint()
        m = self.ceilings.ocp_margin
        if a < sp + m - EPS:
            raise ConflictError(f"OCP {a} A is less than {m} A above the current setpoint {sp} A; "
                                "lower the setpoint first")
        got = {}
        cycled = self._cycled(lambda: got.update(ocp=self.inst.set_ocp(a)), live)
        return {**got, "cycled": cycled}

    def set_lock(self, locked) -> dict:
        locked = _bool(locked, "locked")
        got = self.inst.set_lock(locked)
        return {"locked": locked if got is None else got, "verified": got is not None}
