# SPDX-License-Identifier: Apache-2.0
"""Configuration, read from a TOML file (``/etc/bench-psu/config.toml``).

Every key has a default, so a config written by an older installer keeps
working after an upgrade.
"""

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
    # *IDN? must start with this (case-insensitive) or the daemon sends nothing else
    expect_idn_prefix: str = "KIPRIM,DC620S"
    expect_serial: str = ""  # empty = any serial number
    poll_interval: float = 0.25
    setpoint_interval: float = 5.0
    stale_after: float = 3.0  # seconds without a successful poll before the link counts as stale
    sample_interval_on: float = 2.0  # journal "sample" lines while the output is on (0 = never)
    sample_interval_off: float = 60.0  # ... and while it is off (0 = never)
    listen: str = "0.0.0.0"
    port: int = 8100
    token_file: str = "/etc/bench-psu/token"
    write_allow_from: list = field(default_factory=list)  # empty = any address with the token
    allowed_hosts: list = field(default_factory=list)  # extra Host names on top of IPs, localhost, hostname, FQDN
    request_timeout: float = 10.0  # socket timeout for one HTTP connection
    ceilings: Ceilings = field(default_factory=Ceilings)
    output_off_on_exit: bool = False
    mcp: bool = True
    path: str | None = None

    def write_networks(self):
        return [ipaddress.ip_network(n, strict=False) for n in self.write_allow_from]


# section -> key -> (type, attribute name)
_SECTIONS = {
    "serial": {"device": (str, "device"), "reply_timeout": (float, "reply_timeout"),
               "setter_error_window": (float, "setter_error_window"),
               "lost_after": (int, "lost_after"), "reconnect_interval": (float, "reconnect_interval"),
               "expect_idn_prefix": (str, "expect_idn_prefix"), "expect_serial": (str, "expect_serial")},
    "poll": {"interval": (float, "poll_interval"), "setpoint_interval": (float, "setpoint_interval"),
             "stale_after": (float, "stale_after"), "sample_interval_on": (float, "sample_interval_on"),
             "sample_interval_off": (float, "sample_interval_off")},
    "http": {"listen": (str, "listen"), "port": (int, "port"), "token_file": (str, "token_file"),
             "write_allow_from": (list, "write_allow_from"), "allowed_hosts": (list, "allowed_hosts"),
             "request_timeout": (float, "request_timeout"), "mcp": (bool, "mcp")},
    "limits": {"max_voltage": (float, "max_voltage"), "max_current": (float, "max_current"),
               "ovp_margin": (float, "ovp_margin"), "ocp_margin": (float, "ocp_margin")},
    "behaviour": {"output_off_on_exit": (bool, "output_off_on_exit")},
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
    limits = {"max_voltage": cfg.ceilings.max_voltage, "max_current": cfg.ceilings.max_current,
              "ovp_margin": cfg.ceilings.ovp_margin, "ocp_margin": cfg.ceilings.ocp_margin}
    for section, keys in _SECTIONS.items():
        values = data.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"{p}: [{section}] must be a table")
        for key, value in values.items():
            if key not in keys:
                raise ValueError(f"{p}: unknown key [{section}] {key}")
            typ, attr = keys[key]
            if typ is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, typ) or (typ is not bool and isinstance(value, bool)):
                raise ValueError(f"{p}: [{section}] {key} must be {typ.__name__}")
            if typ is list and not all(isinstance(x, str) for x in value):
                raise ValueError(f"{p}: [{section}] {key} must be a list of strings")
            if section == "limits":
                limits[attr] = value
            else:
                setattr(cfg, attr, value)
    cfg.ceilings = Ceilings(**limits)
    cfg.write_networks()  # validate
    if cfg.poll_interval < 0.05:
        raise ValueError("poll interval below 0.05 s would starve the CH340")
    if cfg.stale_after <= cfg.poll_interval:
        raise ValueError("stale_after must be longer than the poll interval")
    if not 1 <= cfg.port <= 65535:
        raise ValueError("port must be 1-65535")
    return cfg
