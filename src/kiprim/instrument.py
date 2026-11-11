# SPDX-License-Identifier: Apache-2.0
"""The supply's command table as methods.

Every setter writes, then reads the value back and raises
:class:`ReadbackMismatch` unless it matches: the supply answers nothing on
success, so silence alone proves nothing.

This layer sends exactly what it is asked to. Ceilings, the
output-off/set/output-on order for current changes and the consistency rules
between setpoints and protection limits live in :mod:`kiprim.control`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import parse
from .transport import DeviceError, Transport, TransportError

# Resolution of the DC620S per its manual: 10 mV and 1 mA. Values are rounded
# to this before they are sent, and readback must match to within half a step.
VOLT_STEP = 0.01
CURR_STEP = 0.001

# What the hardware accepts, per the manual (setpoints) and the factory
# readback on firmware V2.3.0 (limits: 63 V / 21 A). Host ceilings are lower.
DEVICE_MAX_VOLTAGE = 60.0
DEVICE_MAX_CURRENT = 20.0
DEVICE_MAX_OVP = 63.0
DEVICE_MAX_OCP = 21.0


class WrongDevice(TransportError):
    """The port answered ``*IDN?`` with an identity that is not the expected supply."""


def check_identity(ident: parse.Identity, prefix: str, serial: str = "") -> None:
    """Raise :class:`WrongDevice` unless ``*IDN?`` starts with ``prefix`` and,
    when ``serial`` is set, carries that serial number. Nothing but ``*IDN?``
    may be sent to a port before this passes."""
    if prefix and not ident.raw.upper().startswith(prefix.upper()):
        raise WrongDevice(f"*IDN? is {ident.raw!r}, expected it to start with {prefix!r}")
    if serial and ident.serial != serial:
        raise WrongDevice(f"serial number is {ident.serial!r}, expected {serial!r}")


class ReadbackMismatch(TransportError):
    def __init__(self, what: str, wanted, got):
        super().__init__(f"{what}: wrote {wanted!r}, read back {got!r}")
        self.what, self.wanted, self.got = what, wanted, got


def quantize(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def fmt(value: float, step: float) -> str:
    decimals = 2 if step >= 0.01 else 3
    return f"{quantize(value, step):.{decimals}f}"


@dataclass
class Setpoints:
    voltage: float
    current: float
    ovp: float
    ocp: float


class Instrument:
    def __init__(self, transport: Transport):
        self.t = transport
        self.has_info: bool | None = None  # MEAS:ALL:INFO? supported?
        self.has_lock_query: bool | None = None  # SYST:LOCK? supported?

    # ---- queries -------------------------------------------------------
    def idn(self) -> parse.Identity:
        return parse.parse_idn(self.t.query("*IDN?"))

    def output(self) -> bool:
        return parse.parse_bool(self.t.query("OUTP?"))

    def voltage_setpoint(self) -> float:
        return parse.parse_float(self.t.query("VOLT?"))

    def current_setpoint(self) -> float:
        return parse.parse_float(self.t.query("CURR?"))

    def ovp_limit(self) -> float:
        return parse.parse_float(self.t.query("VOLT:LIM?"))

    def ocp_limit(self) -> float:
        return parse.parse_float(self.t.query("CURR:LIM?"))

    def setpoints(self) -> Setpoints:
        return Setpoints(
            self.voltage_setpoint(), self.current_setpoint(), self.ovp_limit(), self.ocp_limit()
        )

    def measure_voltage(self) -> float:
        return parse.parse_float(self.t.query("MEAS:VOLT?"))

    def measure_current(self) -> float:
        return parse.parse_float(self.t.query("MEAS:CURR?"))

    def measure_power(self) -> float:
        return parse.parse_float(self.t.query("MEAS:POW?"))

    def measure_all(self) -> parse.MeasAll:
        return parse.parse_meas_all(self.t.query("MEAS:ALL?"))

    def measure_info(self) -> parse.MeasInfo | None:
        """``MEAS:ALL:INFO?``, or None when this firmware answers ``ERR``."""
        if self.has_info is False:
            return None
        try:
            info = parse.parse_meas_info(self.t.query("MEAS:ALL:INFO?"))
        except DeviceError:
            self.has_info = False
            return None
        self.has_info = True
        return info

    def lock_state(self) -> bool | None:
        """``SYST:LOCK?`` if the firmware has it, else None (not documented)."""
        if self.has_lock_query is False:
            return None
        try:
            state = parse.parse_bool(self.t.query("SYST:LOCK?"))
        except (DeviceError, parse.ParseError):
            self.has_lock_query = False
            return None
        self.has_lock_query = True
        return state

    # ---- setters (write, read back, compare) ---------------------------
    def _set_number(self, command: str, query, value: float, step: float, what: str) -> float:
        text = fmt(value, step)
        self.t.write(f"{command} {text}")
        got = query()
        if abs(got - float(text)) > step / 2 + 1e-9:
            raise ReadbackMismatch(what, float(text), got)
        return got

    def set_voltage(self, volts: float) -> float:
        return self._set_number("VOLT", self.voltage_setpoint, volts, VOLT_STEP, "VOLT")

    def set_current(self, amps: float) -> float:
        """Raw ``CURR`` write. Use :meth:`Controller.set_current` instead."""
        return self._set_number("CURR", self.current_setpoint, amps, CURR_STEP, "CURR")

    def set_ovp(self, volts: float) -> float:
        return self._set_number("VOLT:LIM", self.ovp_limit, volts, VOLT_STEP, "VOLT:LIM")

    def set_ocp(self, amps: float) -> float:
        return self._set_number("CURR:LIM", self.ocp_limit, amps, CURR_STEP, "CURR:LIM")

    def set_output(self, on: bool) -> bool:
        self.t.write("OUTP ON" if on else "OUTP OFF")
        got = self.output()
        if got != on:
            raise ReadbackMismatch("OUTP", on, got)
        return got

    def set_lock(self, locked: bool) -> bool | None:
        """Front-panel lock. Returns the read-back state, or None when the
        firmware has no ``SYST:LOCK?`` and the write cannot be verified."""
        self.t.write("SYST:LOCK ON" if locked else "SYST:LOCK OFF")
        got = self.lock_state()
        if got is not None and got != locked:
            raise ReadbackMismatch("SYST:LOCK", locked, got)
        return got
