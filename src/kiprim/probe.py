# SPDX-License-Identifier: Apache-2.0
"""Conformance probe: run the protocol table against a supply and tabulate.

Levels, each opt-in on top of the previous one:

``queries`` (default)
    Every query in the table. Changes nothing on the supply.
``setters``
    Requires the output to be OFF. Writes every setpoint and limit back with
    the value it already has (an identity write) and checks the readback, then
    ``SYST:LOCK ON`` / ``SYST:LOCK OFF``, then ``SYST:REM`` / ``SYST:LOC``.
    The supply ends as it started.
``output``
    Requires the output to be OFF and open terminals or a resistive load.
    ``OUTP ON``, reads the mode, ``OUTP OFF``.
``ocp_quirk``
    Requires the output to be OFF and open terminals or a resistive load.
    ``OUTP ON``, then *live* ``CURR`` and ``CURR:LIM`` writes of the unchanged
    values, checking after each whether the output dropped or OCP tripped;
    ends with ``OUTP OFF``. Only meaningful with current flowing.

``*RST`` is never sent.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from . import parse
from .instrument import CURR_STEP, VOLT_STEP, Instrument, fmt
from .transport import DeviceError, ReplyTimeout, TransportError

QUERIES = [
    ("*IDN?", parse.parse_idn),
    ("OUTP?", parse.parse_bool),
    ("VOLT?", parse.parse_float),
    ("CURR?", parse.parse_float),
    ("VOLT:LIM?", parse.parse_float),
    ("CURR:LIM?", parse.parse_float),
    ("MEAS:VOLT?", parse.parse_float),
    ("MEAS:CURR?", parse.parse_float),
    ("MEAS:POW?", parse.parse_float),
    ("MEAS:ALL?", parse.parse_meas_all),
    ("MEAS:ALL:INFO?", parse.parse_meas_info),
    ("SYST:STAT?", None),
]
# SYST:LOCK? is not in any manual; it is only tried, as the readback of a
# lock write, at the "setters" level.

LEVELS = ("queries", "setters", "output", "ocp_quirk")


@dataclass
class Row:
    command: str
    kind: str  # query | setter
    reply: str  # raw reply, "(silent)", "(timeout)"
    result: str  # ok | err | timeout | mismatch | bad-format | skipped | info
    note: str = ""
    ms: float | None = None


class ProbeRefused(RuntimeError):
    """The probe would not start (nothing but queries was sent)."""


class ProbeAborted(RuntimeError):
    """The probe failed part-way; ``rows`` holds what ran, including the cleanup."""

    def __init__(self, message: str, rows: list):
        super().__init__(message)
        self.rows = rows


def _query_row(inst: Instrument, command: str, parser) -> Row:
    t0 = time.monotonic()
    try:
        reply = inst.t.query(command)
    except DeviceError:
        return Row(command, "query", "ERR", "err", ms=_ms(t0))
    except ReplyTimeout:
        return Row(command, "query", "(timeout)", "timeout", ms=_ms(t0))
    ms = _ms(t0)
    if parser is None:
        return Row(command, "query", reply, "info", ms=ms)
    try:
        parsed = parser(reply)
    except parse.ParseError as exc:
        return Row(command, "query", reply, "bad-format", str(exc), ms=ms)
    note = ""
    if command == "MEAS:ALL?":
        note = f"{3 if parsed.power is not None else 2} fields"
    elif command == "MEAS:ALL:INFO?":
        note = f"mode {parsed.mode_code} = {parsed.mode}"
    return Row(command, "query", reply, "ok", note, ms)


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)


def _setter_row(inst: Instrument, command: str, readback: str | None, expect) -> Row:
    t0 = time.monotonic()
    try:
        inst.t.write(command)
    except DeviceError:
        return Row(command, "setter", "ERR", "err", ms=_ms(t0))
    ms = _ms(t0)
    if readback is None:
        return Row(command, "setter", "(silent)", "ok", "no readback query", ms)
    try:
        got = inst.t.query(readback)
    except DeviceError:
        return Row(command, "setter", "(silent)", "ok", f"{readback} -> ERR, unverified", ms)
    except ReplyTimeout:
        return Row(command, "setter", "(silent)", "timeout", f"{readback} -> no reply", ms)
    try:
        good = expect is None or expect(got)
    except parse.ParseError as exc:
        return Row(command, "setter", "(silent)", "bad-format", f"{readback} -> {got}: {exc}", ms)
    if not good:
        return Row(command, "setter", "(silent)", "mismatch", f"{readback} -> {got}", ms)
    return Row(command, "setter", "(silent)", "ok", f"{readback} -> {got}", ms)


def _near(target: float, step: float):
    return lambda reply: abs(parse.parse_float(reply) - target) <= step / 2 + 1e-9


def _cleanup(inst: Instrument, rows: list[Row], command: str, readback: str | None, expect) -> None:
    """Best-effort restore after an exception; recorded, never raised."""
    try:
        row = _setter_row(inst, command, readback, expect)
        row.note = f"cleanup after error; {row.note}"
    except Exception as exc:  # the link may be gone: say so and move on
        row = Row(command, "setter", "", "cleanup-failed", f"{type(exc).__name__}: {exc}")
    rows.append(row)


def run(inst: Instrument, level: str = "queries", ceilings=None) -> list[Row]:
    """``ceilings`` (a :class:`kiprim.control.Ceilings`) is required for the
    levels that switch the output on: they refuse while a setpoint is above a
    ceiling or too close to its protection limit.

    Exception-safe: if anything raises after ``SYST:LOCK ON``, ``SYST:REM`` or
    ``OUTP ON``, the matching ``SYST:LOCK OFF``, ``SYST:LOC`` or ``OUTP OFF``
    is still sent before the exception propagates."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    rows = [_query_row(inst, c, p) for c, p in QUERIES]
    rows.append(Row("*RST", "setter", "", "skipped", "never sent by rule"))
    if level == "queries":
        return rows

    if inst.output():
        raise ProbeRefused("setter probe needs the output OFF first (kiprim off)")
    sp = inst.setpoints()
    if level in ("output", "ocp_quirk"):
        if ceilings is None:
            raise ProbeRefused(f"level {level!r} switches the output on and needs the host ceilings")
        if sp.voltage > ceilings.max_voltage or sp.current > ceilings.max_current:
            raise ProbeRefused(f"setpoints {sp.voltage} V / {sp.current} A are above the host ceilings "
                               f"{ceilings.max_voltage} V / {ceilings.max_current} A; lower them first")
        if sp.voltage > sp.ovp - ceilings.ovp_margin or sp.current > sp.ocp - ceilings.ocp_margin:
            raise ProbeRefused(f"setpoints {sp.voltage} V / {sp.current} A are within the margin of "
                               f"OVP {sp.ovp} V / OCP {sp.ocp} A; fix the limits first")

    for cmd, query, value, step in (
        ("VOLT", "VOLT?", sp.voltage, VOLT_STEP),
        ("CURR", "CURR?", sp.current, CURR_STEP),
        ("VOLT:LIM", "VOLT:LIM?", sp.ovp, VOLT_STEP),
        ("CURR:LIM", "CURR:LIM?", sp.ocp, CURR_STEP),
    ):
        rows.append(_setter_row(inst, f"{cmd} {fmt(value, step)}", query, _near(value, step)))
    after = inst.setpoints()
    if after != sp:
        rows.append(Row("(setpoints)", "setter", "", "mismatch", f"before {sp}, after {after}"))

    pending = []  # (command, readback, expect) to send if something raises
    try:
        pending.append(("SYST:LOCK OFF", "SYST:LOCK?", lambda r: not parse.parse_bool(r)))
        rows.append(_setter_row(inst, "SYST:LOCK ON", "SYST:LOCK?", parse.parse_bool))
        rows.append(_setter_row(inst, "SYST:LOCK OFF", "SYST:LOCK?", lambda r: not parse.parse_bool(r)))
        pending.pop()
        pending.append(("SYST:LOC", None, None))
        rows.append(_setter_row(inst, "SYST:REM", None, None))
        rows.append(_setter_row(inst, "SYST:LOC", None, None))
        pending.pop()
        if level == "setters":
            return rows

        pending.append(("OUTP OFF", "OUTP?", lambda r: not parse.parse_bool(r)))
        rows.append(_setter_row(inst, "OUTP ON", "OUTP?", parse.parse_bool))
        time.sleep(0.5)
        rows.append(_query_row(inst, "MEAS:ALL:INFO?", parse.parse_meas_info))
        if level == "ocp_quirk":
            # The reported trip: a write with the output on. Unchanged values,
            # so a trip (if any) is the write itself, not a new limit.
            for cmd, query, value in (("CURR", "CURR?", sp.current), ("CURR:LIM", "CURR:LIM?", sp.ocp)):
                rows.append(_setter_row(inst, f"{cmd} {fmt(value, CURR_STEP)}", query,
                                        _near(value, CURR_STEP)))
                time.sleep(0.5)
                still_on = inst.output()
                info = inst.measure_info()
                tripped = info.ocp_tripped if info else None
                rows.append(Row(
                    f"(live {cmd} quirk)", "setter", "", "info",
                    f"output {'stayed ON' if still_on else 'DROPPED'}, OCP flag {tripped}"
                    + ("; quirk NOT reproduced" if still_on and not tripped else "; quirk reproduced"),
                ))
                if not still_on:
                    break
        rows.append(_setter_row(inst, "OUTP OFF", "OUTP?", lambda r: not parse.parse_bool(r)))
        pending.pop()
        return rows
    except Exception as exc:
        for command, readback, expect in reversed(pending):
            _cleanup(inst, rows, command, readback, expect)
        raise ProbeAborted(f"probe aborted ({type(exc).__name__}: {exc}); cleanup rows included",
                           rows) from exc
    except BaseException:  # Ctrl-C, SystemExit: still undo, then stop
        for command, readback, expect in reversed(pending):
            _cleanup(inst, rows, command, readback, expect)
        raise


def rows_as_dicts(rows: list[Row]) -> list[dict]:
    return [asdict(r) for r in rows]


def markdown(rows: list[dict]) -> str:
    out = ["| Command | Kind | Reply | Result | Note | ms |", "|---|---|---|---|---|---|"]
    for r in rows:
        reply = r["reply"].replace("|", "\\|")
        ms = "" if r.get("ms") is None else f"{r['ms']:g}"
        out.append(f"| `{r['command']}` | {r['kind']} | `{reply}` | {r['result']} | {r['note']} | {ms} |")
    return "\n".join(out)


__all__ = ["run", "LEVELS", "ProbeRefused", "ProbeAborted", "markdown", "rows_as_dicts", "TransportError"]
