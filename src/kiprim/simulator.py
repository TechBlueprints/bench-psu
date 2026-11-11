# SPDX-License-Identifier: Apache-2.0
"""A simulated DC620S behind the :class:`kiprim.transport.Port` interface.

Defaults model what the real unit (firmware V2.3.0) answered on 2026-11-11:
silent setters, ``ERR`` for unknown commands, ``OUTP?`` as ``ON``/``OFF``,
``MEAS:ALL?`` as two comma-separated fields, ``MEAS:ALL:INFO?`` as seven
fields with word flags and a mode code (0 standby, 1 CV, 2 CC, 3 fault),
``SYST:STAT?`` -> ``ERR``, factory limits 63 V / 21 A, replies in 5-14 ms
when ``realistic_timing`` is on.

Behaviour not yet seen on the real unit is behind switches:

* ``ocp_quirk``: a ``CURR`` write while the output is on trips OCP;
* ``meas_all_fields`` (2 or 3) and ``separator`` (``","`` or ``" "``);
* ``outp_digits``: ``OUTP?`` answers ``1``/``0``;
* ``info_supported``: ``MEAS:ALL:INFO?`` answers ``ERR``;
* ``lock_query``: ``SYST:LOCK?`` exists;
* ``drop_replies``: the next N commands get no reply (timeouts);
* ``readback_offset``: setpoint queries read back off by this much.

The load is a resistor (``load_ohms``; None = open terminals).
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque


class SimulatedSupply:
    def __init__(self, **switches):
        self.model = "DC620S"
        self.serial = "SIM00001"
        self.firmware = "V2.3.0"
        self.voltage = 10.0
        self.current = 2.0
        self.ovp = 63.0
        self.ocp = 21.0
        self.output = False
        self.locked = False
        self.ovp_tripped = False
        self.ocp_tripped = False
        self.otp_tripped = False
        self.load_ohms: float | None = None
        # switches
        self.ocp_quirk = False
        self.meas_all_fields = 2
        self.separator = ","
        self.outp_digits = False
        self.info_supported = True
        self.lock_query = False
        self.drop_replies = 0
        self.readback_offset = 0.0
        self.realistic_timing = False
        for key, value in switches.items():
            if not hasattr(self, key):
                raise TypeError(f"unknown simulator switch {key!r}")
            setattr(self, key, value)
        self.log: list[str] = []  # every command received, in order
        self.lock = threading.Lock()

    # ---- physics -------------------------------------------------------
    def _operating_point(self) -> tuple[float, float, int]:
        if self.ovp_tripped or self.ocp_tripped or self.otp_tripped:
            return 0.0, 0.0, 3 if self.output else 0
        if not self.output:
            return 0.001, 0.0, 0
        if self.load_ohms is None:
            return self.voltage, 0.0, 1
        i = self.voltage / self.load_ohms
        if i <= self.current:
            return self.voltage, round(i, 3), 1
        return round(self.current * self.load_ohms, 3), self.current, 2

    def _check_protection(self) -> None:
        if not self.output:
            return
        v, i, _ = self._operating_point()
        if v > self.ovp:
            self.ovp_tripped, self.output = True, False
        elif i > self.ocp:
            self.ocp_tripped, self.output = True, False

    # ---- protocol ------------------------------------------------------
    def handle(self, line: str) -> str | None:
        with self.lock:
            self.log.append(line)
            if self.drop_replies > 0:
                self.drop_replies -= 1
                return None
            try:
                return self._dispatch(line.strip())
            except (ValueError, IndexError):
                return "ERR"

    def _fmt(self, *values) -> str:
        return self.separator.join(f"{v:.3f}" for v in values)

    def _dispatch(self, cmd: str) -> str | None:
        head, _, arg = cmd.partition(" ")
        head = head.upper()
        arg = arg.strip()
        v, i, mode = self._operating_point()
        off = self.readback_offset
        if head == "*IDN?":
            return f"KIPRIM,{self.model},{self.serial},FV:{self.firmware}"
        if head == "OUTP?":
            return ("1" if self.output else "0") if self.outp_digits else ("ON" if self.output else "OFF")
        if head == "VOLT?":
            return f"{self.voltage + off:.3f}"
        if head == "CURR?":
            return f"{self.current + off:.3f}"
        if head == "VOLT:LIM?":
            return f"{self.ovp + off:.3f}"
        if head == "CURR:LIM?":
            return f"{self.ocp + off:.3f}"
        if head == "MEAS:VOLT?":
            return f"{v:.3f}"
        if head == "MEAS:CURR?":
            return f"{i:.3f}"
        if head == "MEAS:POW?":
            return f"{v * i:.3f}"
        if head == "MEAS:ALL?":
            return self._fmt(v, i) if self.meas_all_fields == 2 else self._fmt(v, i, v * i)
        if head == "MEAS:ALL:INFO?":
            if not self.info_supported:
                return "ERR"
            word = lambda b: "ON" if b else "OFF"  # noqa: E731
            return self.separator.join([
                f"{v:.3f}", f"{i:.3f}", f"{v * i:.3f}",
                word(self.ovp_tripped), word(self.ocp_tripped), word(self.otp_tripped), str(mode),
            ])
        if head == "SYST:LOCK?":
            if not self.lock_query:
                return "ERR"
            return "ON" if self.locked else "OFF"
        if head == "SYST:STAT?":
            return "ERR"
        if head == "VOLT" and arg:
            value = float(arg)
            if not 0 <= value <= 60:
                return "ERR"
            self.voltage = round(value, 2)
            self._check_protection()
            return None
        if head == "CURR" and arg:
            value = float(arg)
            if not 0 <= value <= 20:
                return "ERR"
            self.current = round(value, 3)
            if self.ocp_quirk and self.output:
                self.ocp_tripped, self.output = True, False
            return None
        if head == "VOLT:LIM" and arg:
            value = float(arg)
            if not 0 <= value <= 63:
                return "ERR"
            self.ovp = round(value, 2)
            self._check_protection()
            return None
        if head == "CURR:LIM" and arg:
            value = float(arg)
            if not 0 <= value <= 21:
                return "ERR"
            self.ocp = round(value, 3)
            self._check_protection()
            return None
        if head == "OUTP" and arg.upper() in ("ON", "OFF", "1", "0"):
            on = arg.upper() in ("ON", "1")
            if on:
                self.ovp_tripped = self.ocp_tripped = self.otp_tripped = False
            self.output = on
            self._check_protection()
            return None
        if head == "SYST:LOCK" and arg.upper() in ("ON", "OFF"):
            self.locked = arg.upper() == "ON"
            return None
        if head in ("SYST:REM", "SYST:LOC") and not arg:
            return None
        if head == "*RST":
            self.__init__()  # factory defaults; the real thing: never from software
            return None
        return "ERR"

    def setters_received(self) -> list[str]:
        """Commands in the log that are not queries."""
        return [c for c in self.log if "?" not in c]


class SimPort:
    """:class:`Port` over a :class:`SimulatedSupply`."""

    def __init__(self, supply: SimulatedSupply | None = None):
        self.supply = supply or SimulatedSupply()
        self._pending: deque[bytes] = deque()
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            from .transport import LinkLost
            raise LinkLost("simulated port is closed")
        for raw in data.split(b"\n"):
            if not raw:
                continue
            reply = self.supply.handle(raw.decode("ascii"))
            if reply is not None:
                self._pending.append(reply.encode("ascii"))

    def read_line(self, timeout: float) -> bytes | None:
        if self._pending:
            if self.supply.realistic_timing:
                time.sleep(random.uniform(0.005, 0.014))
            return self._pending.popleft()
        # The real port would block for the whole timeout; the simulator
        # returns at once unless realistic timing is on.
        if self.supply.realistic_timing:
            time.sleep(timeout)
        return None

    def flush_input(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self.closed = True
