# SPDX-License-Identifier: Apache-2.0
"""Configuration, read from a TOML file (``/etc/bench-psu/config.toml``)."""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .control import Ceilings

DEFAULT_PATH = "/etc/bench-psu/config.toml"


@dataclass
class Config:
    device: str = "/dev/kiprim"
    reply_timeout: float = 0.5
    setter_error_window: float = 0.15
    lost_after: int = 3
    reconnect_interval: float = 2.0
    poll_interval: float = 0.25
    setpoint_interval: float = 5.0
    listen: str = "0.0.0.0"
    port: int = 8100
    token_file: str = "/etc/bench-psu/token"
    write_allow_from: list = field(default_factory=list)  # empty = any address with the token
    ceilings: Ceilings = field(default_factory=Ceilings)
    output_off_on_exit: bool = False
    mcp: bool = True
    path: str | None = None

    def write_networks(self):
        return [ipaddress.ip_network(n, strict=False) for n in self.write_allow_from]


_SECTIONS = {
    "serial": {"device": str, "reply_timeout": float, "setter_error_window": float,
               "lost_after": int, "reconnect_interval": float},
    "poll": {"interval": float, "setpoint_interval": float},
    "http": {"listen": str, "port": int, "token_file": str, "write_allow_from": list, "mcp": bool},
    "limits": {"max_voltage": float, "max_current": float},
    "behaviour": {"output_off_on_exit": bool},
}


def load(path: str | None = None) -> Config:
    cfg = Config()
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        if path is not None:
            raise FileNotFoundError(path)
        return cfg
    data = tomllib.loads(p.read_text())
    cfg.path = str(p)
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ValueError(f"{p}: unknown section(s) {sorted(unknown)}")
    max_v, max_a = cfg.ceilings.max_voltage, cfg.ceilings.max_current
    for section, keys in _SECTIONS.items():
        for key, value in data.get(section, {}).items():
            if key not in keys:
                raise ValueError(f"{p}: unknown key [{section}] {key}")
            typ = keys[key]
            if typ is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, typ) or (typ is not bool and isinstance(value, bool)):
                raise ValueError(f"{p}: [{section}] {key} must be {typ.__name__}")
            if section == "limits":
                if key == "max_voltage":
                    max_v = value
                else:
                    max_a = value
            elif section == "poll":
                setattr(cfg, "poll_interval" if key == "interval" else key, value)
            else:
                setattr(cfg, key, value)
    cfg.ceilings = Ceilings(max_v, max_a)
    cfg.write_networks()  # validate
    if cfg.poll_interval < 0.05:
        raise ValueError("poll interval below 0.05 s would starve the CH340")
    return cfg
