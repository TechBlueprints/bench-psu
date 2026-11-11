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
    ``OUTP ON``, then a *live* ``CURR`` write of the unchanged value, then
    checks whether the output dropped or OCP tripped; ends with ``OUTP OFF``.

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
    pass


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
    if expect is not None and not expect(got):
        return Row(command, "setter", "(silent)", "mismatch", f"{readback} -> {got}", ms)
    return Row(command, "setter", "(silent)", "ok", f"{readback} -> {got}", ms)


def _near(target: float, step: float):
    return lambda reply: abs(parse.parse_float(reply) - target) <= step / 2 + 1e-9


def run(inst: Instrument, level: str = "queries", ceilings=None) -> list[Row]:
    """``ceilings`` (a :class:`kiprim.control.Ceilings`) is required for the
    levels that switch the output on: they refuse while a setpoint is above it."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    rows = [_query_row(inst, c, p) for c, p in QUERIES]
    rows.append(Row("*RST", "setter", "", "skipped", "never sent by rule"))
    if level == "queries":
        return rows

    if inst.output():
        raise ProbeRefused("setter probe needs the output OFF first (kiprim off)")
    sp = inst.setpoints()
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
    rows.append(_setter_row(inst, "SYST:LOCK ON", "SYST:LOCK?", lambda r: parse.parse_bool(r)))
    rows.append(_setter_row(inst, "SYST:LOCK OFF", "SYST:LOCK?", lambda r: not parse.parse_bool(r)))
    rows.append(_setter_row(inst, "SYST:REM", None, None))
    rows.append(_setter_row(inst, "SYST:LOC", None, None))
    if level == "setters":
        return rows

    if ceilings is None:
        raise ProbeRefused(f"level {level!r} switches the output on and needs the host ceilings")
    if sp.voltage > ceilings.max_voltage or sp.current > ceilings.max_current:
        raise ProbeRefused(f"setpoints {sp.voltage} V / {sp.current} A are above the host ceilings "
                           f"{ceilings.max_voltage} V / {ceilings.max_current} A; lower them first")

    rows.append(_setter_row(inst, "OUTP ON", "OUTP?", parse.parse_bool))
    time.sleep(0.5)
    rows.append(_query_row(inst, "MEAS:ALL:INFO?", parse.parse_meas_info))
    if level == "ocp_quirk":
        rows.append(_setter_row(inst, f"CURR {fmt(sp.current, CURR_STEP)}", "CURR?", _near(sp.current, CURR_STEP)))
        time.sleep(0.5)
        still_on = inst.output()
        info = inst.measure_info()
        tripped = info.ocp_tripped if info else None
        rows.append(Row(
            "(live CURR quirk)", "setter", "", "info",
            f"output {'stayed ON' if still_on else 'DROPPED'}, OCP flag {tripped}"
            + ("; quirk NOT reproduced" if still_on and not tripped else "; quirk reproduced"),
        ))
    rows.append(_setter_row(inst, "OUTP OFF", "OUTP?", lambda r: not parse.parse_bool(r)))
    return rows


def rows_as_dicts(rows: list[Row]) -> list[dict]:
    return [asdict(r) for r in rows]


def markdown(rows: list[dict]) -> str:
    out = ["| Command | Kind | Reply | Result | Note | ms |", "|---|---|---|---|---|---|"]
    for r in rows:
        reply = r["reply"].replace("|", "\\|")
        ms = "" if r.get("ms") is None else f"{r['ms']:g}"
        out.append(f"| `{r['command']}` | {r['kind']} | `{reply}` | {r['result']} | {r['note']} | {ms} |")
    return "\n".join(out)


__all__ = ["run", "LEVELS", "ProbeRefused", "markdown", "rows_as_dicts", "TransportError"]
