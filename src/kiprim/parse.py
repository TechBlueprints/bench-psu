# SPDX-License-Identifier: Apache-2.0
"""Tolerant parsers for the replies of Kiprim / OWON-family SCPI supplies.

Firmware differs across the family: ``MEAS:ALL?`` answers two or three fields,
comma- or space-separated; ``OUTP?`` answers ``ON``/``OFF`` or ``1``/``0``;
the protection flags in ``MEAS:ALL:INFO?`` are words or digits. Every parser
here accepts all of those and raises :class:`ParseError` on anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPLIT = re.compile(r"[,\s]+")

MODE_NAMES = {0: "standby", 1: "cv", 2: "cc", 3: "fault"}


class ParseError(ValueError):
    """A reply did not have the expected shape."""


def fields(reply: str) -> list[str]:
    reply = reply.strip()
    if not reply:
        return []
    return [f for f in _SPLIT.split(reply) if f]


def parse_float(reply: str) -> float:
    text = reply.strip()
    # Some firmware appends a unit letter ("13.800V"); accept and drop it.
    text = re.sub(r"\s*[VAWvaw]$", "", text)
    try:
        return float(text)
    except ValueError:
        raise ParseError(f"not a number: {reply!r}") from None


_TRUE = {"ON", "1", "TRUE", "YES"}
_FALSE = {"OFF", "0", "FALSE", "NO"}


def parse_bool(reply: str) -> bool:
    text = reply.strip().upper()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ParseError(f"not ON/OFF/1/0: {reply!r}")


@dataclass(frozen=True)
class Identity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str

    def as_dict(self) -> dict:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
            "raw": self.raw,
        }


def parse_idn(reply: str) -> Identity:
    raw = reply.strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 2 or not parts[0]:
        raise ParseError(f"unexpected *IDN? reply: {reply!r}")
    while len(parts) < 4:
        parts.append("")
    firmware = parts[3]
    if firmware.upper().startswith("FV:"):
        firmware = firmware[3:]
    return Identity(parts[0], parts[1], parts[2], firmware, raw)


@dataclass(frozen=True)
class MeasAll:
    voltage: float
    current: float
    power: float | None  # only when the firmware sends a third field


def parse_meas_all(reply: str) -> MeasAll:
    f = fields(reply)
    if len(f) not in (2, 3):
        raise ParseError(f"MEAS:ALL? wants 2 or 3 fields, got {len(f)}: {reply!r}")
    v, i = parse_float(f[0]), parse_float(f[1])
    p = parse_float(f[2]) if len(f) == 3 else None
    return MeasAll(v, i, p)


@dataclass(frozen=True)
class MeasInfo:
    voltage: float
    current: float
    power: float
    ovp_tripped: bool
    ocp_tripped: bool
    otp_tripped: bool
    mode_code: int

    @property
    def mode(self) -> str:
        return MODE_NAMES.get(self.mode_code, "unknown")


def parse_meas_info(reply: str) -> MeasInfo:
    f = fields(reply)
    if len(f) != 7:
        raise ParseError(f"MEAS:ALL:INFO? wants 7 fields, got {len(f)}: {reply!r}")
    try:
        mode = int(f[6])
    except ValueError:
        raise ParseError(f"mode field is not an integer: {reply!r}") from None
    return MeasInfo(
        parse_float(f[0]),
        parse_float(f[1]),
        parse_float(f[2]),
        parse_bool(f[3]),
        parse_bool(f[4]),
        parse_bool(f[5]),
        mode,
    )
