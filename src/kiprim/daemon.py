# SPDX-License-Identifier: Apache-2.0
"""``bench-psud``: the only process that talks to the supply.

One worker thread owns the serial port. It polls the supply a few times a
second, keeps the latest state, and runs write jobs queued by the HTTP API
and the MCP endpoint, one at a time, between polls.

Starting, stopping, restarting or reconnecting sends **queries only**. The
output changes only when a client asks for it, or on exit when
``output_off_on_exit`` is set in the config (default off). Before anything
but ``*IDN?`` is sent, the identity must match ``expect_idn_prefix`` (and
``expect_serial`` when set).

Every state change, write, refused write and a periodic measurement sample is
one JSON line on stdout, which systemd puts in the journal.
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import queue
import signal
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, mcp, parse, probe
from .config import Config, load
from .control import ConflictError, Controller, CycleError, LimitError
from .instrument import Instrument, ReadbackMismatch, WrongDevice, check_identity
from .transport import (
    DeviceError, LinkLost, PortBusy, ReplyTimeout, Transport, TransportError, TtyPort,
)

_log_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(event: str, **fields) -> None:
    """One JSON object per line. Never call from a signal handler."""
    line = json.dumps({"ts": now_iso(), "event": event, **fields}, separators=(",", ":"), default=str)
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class NotConnected(TransportError):
    pass


@dataclass
class Job:
    name: str
    fn: object
    done: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    cancelled: bool = False
    result: object = None
    error: BaseException | None = None


_LIVE = {"type": "boolean", "default": False,
         "description": "With the output on, write without switching it off and back on first."}

# ---- operations shared by the HTTP API and MCP ---------------------------
# name -> (handler(controller, args) -> dict, JSON schema of args, description)
OPS = {
    "set_voltage": (
        lambda c, a: c.set_voltage(a.get("volts")),
        {"type": "object", "properties": {"volts": {"type": "number", "minimum": 0}},
         "required": ["volts"]},
        "Set the voltage setpoint. Applied live. Refused above the host ceiling or closer than "
        "the OVP margin to the OVP limit.",
    ),
    "set_current": (
        lambda c, a: c.set_current(a.get("amps"), live=a.get("live", False)),
        {"type": "object", "properties": {"amps": {"type": "number", "minimum": 0}, "live": _LIVE},
         "required": ["amps"]},
        "Set the current limit (constant-current level). If the output is on it is switched off, "
        "the value written, and switched back on, unless live=true. Refused above the host ceiling "
        "or closer than the OCP margin to the OCP limit.",
    ),
    "set_voltage_current": (
        lambda c, a: c.set_both(a.get("volts"), a.get("amps"), live=a.get("live", False)),
        {"type": "object", "properties": {
            "volts": {"type": "number", "minimum": 0}, "amps": {"type": "number", "minimum": 0},
            "live": _LIVE},
         "required": ["volts", "amps"]},
        "Set voltage and current together, with at most one output off/on cycle (only when the "
        "current changes while the output is on).",
    ),
    "set_output": (
        lambda c, a: c.set_output(a.get("on")),
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        "Switch the output on or off. Switching on is refused while a setpoint is above a host "
        "ceiling or too close to its protection limit.",
    ),
    "set_ovp": (
        lambda c, a: c.set_ovp(a.get("volts"), live=a.get("live", False)),
        {"type": "object", "properties": {"volts": {"type": "number", "minimum": 0}, "live": _LIVE},
         "required": ["volts"]},
        "Set the over-voltage protection limit (at least the OVP margin above the voltage "
        "setpoint). If the output is on it is cycled off/on unless live=true.",
    ),
    "set_ocp": (
        lambda c, a: c.set_ocp(a.get("amps"), live=a.get("live", False)),
        {"type": "object", "properties": {"amps": {"type": "number", "minimum": 0}, "live": _LIVE},
         "required": ["amps"]},
        "Set the over-current protection limit (at least the OCP margin above the current "
        "setpoint). If the output is on it is cycled off/on unless live=true: writing the OCP "
        "limit with current flowing is the case reported to trip OCP on this firmware family.",
    ),
    "set_lock": (
        lambda c, a: c.set_lock(a.get("locked")),
        {"type": "object", "properties": {"locked": {"type": "boolean"}}, "required": ["locked"]},
        "Lock or unlock the front panel. The firmware has no lock query, so the result is unverified.",
    ),
}

HTTP_ROUTES = {
    "/api/voltage": "set_voltage",
    "/api/current": "set_current",
    "/api/set": "set_voltage_current",
    "/api/output": "set_output",
    "/api/ovp": "set_ovp",
    "/api/ocp": "set_ocp",
    "/api/lock": "set_lock",
}

ERROR_STATUS = [
    (LimitError, HTTPStatus.UNPROCESSABLE_ENTITY, "limit"),
    (ConflictError, HTTPStatus.CONFLICT, "conflict"),
    (probe.ProbeRefused, HTTPStatus.CONFLICT, "conflict"),
    (probe.ProbeAborted, HTTPStatus.BAD_GATEWAY, "probe-aborted"),
    (CycleError, HTTPStatus.BAD_GATEWAY, "cycle-failed"),
    (ReadbackMismatch, HTTPStatus.BAD_GATEWAY, "readback-mismatch"),
    (DeviceError, HTTPStatus.BAD_GATEWAY, "device-error"),
    (WrongDevice, HTTPStatus.SERVICE_UNAVAILABLE, "wrong-device"),
    (NotConnected, HTTPStatus.SERVICE_UNAVAILABLE, "not-connected"),
    (LinkLost, HTTPStatus.SERVICE_UNAVAILABLE, "link-lost"),
    (ReplyTimeout, HTTPStatus.GATEWAY_TIMEOUT, "timeout"),
    (TimeoutError, HTTPStatus.GATEWAY_TIMEOUT, "timeout"),
    (TransportError, HTTPStatus.BAD_GATEWAY, "transport"),
    (ValueError, HTTPStatus.BAD_REQUEST, "bad-request"),
]


def classify(exc: BaseException) -> tuple[HTTPStatus, str]:
    for typ, status, kind in ERROR_STATUS:
        if isinstance(exc, typ):
            return status, kind
    return HTTPStatus.INTERNAL_SERVER_ERROR, "internal"


class Daemon:
    def __init__(self, cfg: Config, port_factory=None):
        self.cfg = cfg
        self.port_factory = port_factory or (lambda: TtyPort(cfg.device))
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.stop_event = threading.Event()
        self.inst: Instrument | None = None
        self.ctl: Controller | None = None
        self.started = now_iso()
        self._lock = threading.Lock()
        self.state = {
            "identity": None, "output": None, "mode": "unknown", "mode_code": None,
            "setpoints": {"voltage": None, "current": None},
            "protection": {"ovp_limit": None, "ocp_limit": None, "ovp_tripped": None,
                           "ocp_tripped": None, "otp_tripped": None},
            "measured": {"voltage": None, "current": None, "power": None},
            "energy": {"wh": 0.0, "ah": 0.0, "output_on_s": 0.0, "since": self.started},
            "front_panel_lock": None,
        }
        self.link = {"state": "connecting", "device": cfg.device, "last_poll": None,
                     "last_poll_mono": None, "consecutive_timeouts": 0, "connects": 0,
                     "last_error": None, "connected_since": None}
        self._poll_window = (time.monotonic(), 0)
        self._last_energy_mono: float | None = None
        self._last_sample_mono = 0.0
        self.poll_hz = 0.0
        self.worker = threading.Thread(target=self._worker, name="psu-worker", daemon=True)

    # ---- public ----------------------------------------------------------
    def start(self) -> None:
        log_event("start", version=__version__, device=self.cfg.device,
                  max_voltage=self.cfg.ceilings.max_voltage, max_current=self.cfg.ceilings.max_current,
                  ovp_margin=self.cfg.ceilings.ovp_margin, ocp_margin=self.cfg.ceilings.ocp_margin,
                  expect_idn_prefix=self.cfg.expect_idn_prefix,
                  output_off_on_exit=self.cfg.output_off_on_exit)
        self.worker.start()

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        self.worker.join(timeout)

    def submit(self, name: str, fn, timeout: float = 15.0):
        job = Job(name, fn)
        self.jobs.put(job)
        if not job.done.wait(timeout):
            with self._lock:
                if not job.started:
                    job.cancelled = True
            if job.cancelled:
                raise TimeoutError(f"{name}: worker busy, request dropped before it was sent")
            job.done.wait()  # it is running: never abandon a write mid-sequence
        if job.error is not None:
            raise job.error
        return job.result

    def run_op(self, name: str, args: dict, source: str) -> dict:
        if name == "reset_counters":
            with self._lock:
                self.state["energy"] = {"wh": 0.0, "ah": 0.0, "output_on_s": 0.0, "since": now_iso()}
            log_event("counters_reset", source=source)
            return {"energy": dict(self.state["energy"])}
        handler = OPS[name][0]
        try:
            result = self.submit(name, lambda: handler(self.ctl, args))
        except Exception as exc:
            _, kind = classify(exc)
            log_event("write_failed", op=name, args=args, source=source, kind=kind, error=str(exc))
            raise
        if name == "set_lock":
            with self._lock:
                self.state["front_panel_lock"] = result.get("locked")
        log_event("write", op=name, args=args, source=source, result=result)
        return result

    def run_probe(self, level: str, source: str) -> list[dict]:
        log_event("probe", level=level, source=source)
        try:
            rows = self.submit("probe", lambda: probe.rows_as_dicts(
                probe.run(self.inst, level, self.cfg.ceilings)), timeout=60.0)
        except probe.ProbeRefused as exc:
            log_event("probe_refused", level=level, source=source, reason=str(exc))
            raise
        except probe.ProbeAborted as exc:
            log_event("probe_failed", level=level, source=source, error=str(exc),
                      cleanup=[r.command for r in exc.rows if "cleanup" in (r.note or "")
                               or r.result == "cleanup-failed"])
            raise
        except Exception as exc:
            log_event("probe_failed", level=level, source=source, error=str(exc))
            raise
        log_event("probe_done", level=level, rows=len(rows),
                  results={r["command"]: r["result"] for r in rows})
        return rows

    def status(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self.state))
            link = dict(self.link)
        mono = link.pop("last_poll_mono")
        age = None if mono is None else time.monotonic() - mono
        link["age_s"] = None if age is None else round(age, 3)
        link["poll_hz"] = round(self.poll_hz, 2)
        link["reconnects"] = max(0, link["connects"] - 1)
        link["worker_alive"] = self.worker.is_alive()
        if link["state"] == "ok" and (age is None or age > self.cfg.stale_after):
            link["state"] = "stale"
        if not link["worker_alive"] and self.worker.ident is not None:
            link["state"] = "worker-dead"
        link["ok"] = link["state"] == "ok"
        warnings = []
        sp = st["setpoints"]
        c = self.cfg.ceilings
        if sp["voltage"] is not None and sp["voltage"] > c.max_voltage:
            warnings.append(f"voltage setpoint {sp['voltage']} V is above the host ceiling {c.max_voltage} V")
        if sp["current"] is not None and sp["current"] > c.max_current:
            warnings.append(f"current setpoint {sp['current']} A is above the host ceiling {c.max_current} A")
        if link["state"] == "stale":
            warnings.append(f"link stale: no successful poll for {link['age_s']} s")
        elif link["state"] != "ok":
            warnings.append(f"link {link['state']}: {link['last_error'] or 'no data yet'}")
        if any(st["protection"][k] for k in ("ovp_tripped", "ocp_tripped", "otp_tripped")):
            warnings.append("a protection has tripped")
        return {
            "time": now_iso(),
            **st,
            "link": link,
            "ceilings": {"max_voltage": c.max_voltage, "max_current": c.max_current,
                         "ovp_margin": c.ovp_margin, "ocp_margin": c.ocp_margin},
            "warnings": warnings,
            "daemon": {"version": __version__, "started": self.started,
                       "output_off_on_exit": self.cfg.output_off_on_exit,
                       "writes_enabled": bool(read_token(self.cfg.token_file))},
        }

    # ---- worker ------------------------------------------------------------
    def _set_link(self, state: str, error: str | None = None) -> None:
        with self._lock:
            previous = self.link["state"]
            self.link["state"] = state
            if error is not None:
                self.link["last_error"] = error
        if previous != state:
            log_event("link", state=state, previous=previous, error=error)

    def _connect(self) -> bool:
        try:
            port = self.port_factory()
        except PortBusy as exc:
            self._set_link("busy", str(exc))
            return False
        except (TransportError, OSError) as exc:
            self._set_link("absent", str(exc))
            return False
        t = Transport(port, self.cfg.reply_timeout, self.cfg.setter_error_window, self.cfg.lost_after)
        inst = Instrument(t)
        try:
            ident = inst.idn()
            check_identity(ident, self.cfg.expect_idn_prefix, self.cfg.expect_serial)
        except WrongDevice as exc:
            t.close()
            self._set_link("wrong-device", str(exc))
            return False
        except (TransportError, parse.ParseError, OSError) as exc:
            t.close()
            self._set_link("no-reply", f"*IDN? failed: {exc}")
            return False
        self.inst, self.ctl = inst, Controller(inst, self.cfg.ceilings)
        with self._lock:
            self.state["identity"] = ident.as_dict()
            self.link["consecutive_timeouts"] = 0
            self.link["connected_since"] = now_iso()
            self.link["connects"] += 1
        self._last_energy_mono = None
        log_event("identity", **ident.as_dict())
        self._refresh(full=True)
        return self.inst is not None

    def _disconnect(self, reason: str) -> None:
        if self.inst is not None:
            try:
                self.inst.t.close()
            except Exception:
                pass
        self.inst = self.ctl = None
        self._last_energy_mono = None
        self._set_link("lost", reason)

    def _drain_jobs(self, error: BaseException) -> None:
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            job.error = error
            job.done.set()

    def _worker(self) -> None:
        next_poll = next_full = 0.0
        while not self.stop_event.is_set():
            try:
                if self.inst is None:
                    if not self._connect():
                        self._drain_jobs(NotConnected(
                            f"supply not connected ({self.link['state']}: {self.link['last_error']})"))
                        self.stop_event.wait(self.cfg.reconnect_interval)
                        continue
                    next_poll = time.monotonic() + self.cfg.poll_interval
                    next_full = time.monotonic() + self.cfg.setpoint_interval
                try:
                    job = self.jobs.get(timeout=max(0.0, next_poll - time.monotonic()))
                except queue.Empty:
                    job = None
                if job is not None:
                    self._run_job(job)
                    if self.inst is not None:
                        self._refresh(full=True)
                        next_full = time.monotonic() + self.cfg.setpoint_interval
                    continue
                full = time.monotonic() >= next_full
                self._refresh(full=full)
                if full:
                    next_full = time.monotonic() + self.cfg.setpoint_interval
                # fixed rate: schedule from the previous slot, not from now
                next_poll += self.cfg.poll_interval
                if next_poll < time.monotonic() - self.cfg.poll_interval:
                    next_poll = time.monotonic() + self.cfg.poll_interval
            except Exception as exc:  # never let the only owner of the port die quietly
                log_event("worker_error", error=f"{type(exc).__name__}: {exc}",
                          where=traceback.format_exc(limit=3).splitlines()[-3:])
                self._disconnect(f"worker error: {type(exc).__name__}: {exc}")
                self.stop_event.wait(self.cfg.reconnect_interval)
        self._shutdown()

    def _run_job(self, job: Job) -> None:
        with self._lock:
            if job.cancelled:
                job.done.set()
                return
            job.started = True
        try:
            if self.inst is None:
                raise NotConnected("supply not connected")
            job.result = job.fn()
        except LinkLost as exc:
            job.error = exc
            self._disconnect(str(exc))
        except BaseException as exc:  # reported to the caller
            job.error = exc
        finally:
            job.done.set()

    def _refresh(self, full: bool) -> None:
        inst = self.inst
        if inst is None:
            return
        try:
            info = inst.measure_info()
            if info is None:
                ma = inst.measure_all()
                power = ma.power if ma.power is not None else inst.measure_power()
                measured = {"voltage": ma.voltage, "current": ma.current, "power": power}
                flags = None
                mode_code = None
            else:
                measured = {"voltage": info.voltage, "current": info.current, "power": info.power}
                flags = {"ovp_tripped": info.ovp_tripped, "ocp_tripped": info.ocp_tripped,
                         "otp_tripped": info.otp_tripped}
                mode_code = info.mode_code
            output = inst.output()
            sp = inst.setpoints() if full else None
        except LinkLost as exc:
            self._disconnect(str(exc))
            return
        except ReplyTimeout as exc:
            with self._lock:
                self.link["consecutive_timeouts"] = inst.t.consecutive_timeouts
                self.link["last_error"] = str(exc)
            return
        except (TransportError, parse.ParseError) as exc:
            with self._lock:
                self.link["last_error"] = str(exc)
            log_event("poll_error", error=str(exc))
            return
        now = time.monotonic()
        changes = []
        with self._lock:
            st = self.state
            # energy: integrate the previous interval while the output was on
            if self._last_energy_mono is not None and st["output"]:
                dt = min(now - self._last_energy_mono, self.cfg.stale_after)
                e = st["energy"]
                e["wh"] = round(e["wh"] + measured["power"] * dt / 3600.0, 6)
                e["ah"] = round(e["ah"] + measured["current"] * dt / 3600.0, 6)
                e["output_on_s"] = round(e["output_on_s"] + dt, 3)
            self._last_energy_mono = now
            if st["output"] != output:
                changes.append(("output", {"output": output, "previous": st["output"]}))
                st["output"] = output
            if mode_code is not None and st["mode_code"] != mode_code:
                changes.append(("mode", {"mode": parse.MODE_NAMES.get(mode_code, "unknown"),
                                         "mode_code": mode_code, "previous": st["mode"]}))
            if mode_code is not None:
                st["mode_code"] = mode_code
                st["mode"] = parse.MODE_NAMES.get(mode_code, "unknown")
            elif not output:
                st["mode"], st["mode_code"] = "standby", None
            if flags is not None:
                old = {k: st["protection"][k] for k in flags}
                if old != flags:
                    changes.append(("protection", {**flags, "previous": old}))
                st["protection"].update(flags)
            st["measured"] = measured
            if sp is not None:
                new = {"voltage": sp.voltage, "current": sp.current}
                lim = {"ovp_limit": sp.ovp, "ocp_limit": sp.ocp}
                old_sp = dict(st["setpoints"])
                old_lim = {k: st["protection"][k] for k in lim}
                if old_sp != new or old_lim != lim:
                    changes.append(("setpoints", {**new, **lim}))
                st["setpoints"] = new
                st["protection"].update(lim)
            self.link["last_poll"] = now_iso()
            self.link["last_poll_mono"] = now
            self.link["consecutive_timeouts"] = 0
            start, count = self._poll_window
            count += 1
            if now - start >= 5.0:
                self.poll_hz = count / (now - start)
                self._poll_window = (now, 0)
            else:
                self._poll_window = (start, count)
            interval = self.cfg.sample_interval_on if output else self.cfg.sample_interval_off
            sample = None
            if interval > 0 and now - self._last_sample_mono >= interval:
                self._last_sample_mono = now
                sample = {**measured, "output": output, "mode": st["mode"],
                          "wh": st["energy"]["wh"], "ah": st["energy"]["ah"]}
        if self.link["state"] != "ok":
            self._set_link("ok")
        for event, fields in changes:
            log_event(event, **fields)
        if sample is not None:
            log_event("sample", **sample)

    def _shutdown(self) -> None:
        self._drain_jobs(NotConnected("daemon is stopping"))
        if self.inst is not None and self.cfg.output_off_on_exit:
            try:
                self.inst.set_output(False)
                log_event("output_off_on_exit", output=False)
            except Exception as exc:
                log_event("output_off_on_exit_failed", error=str(exc))
        if self.inst is not None:
            self.inst.t.close()
        self.inst = self.ctl = None
        log_event("stop")


# ---- auth ------------------------------------------------------------------
def read_token(path: str) -> str | None:
    try:
        with open(path) as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def check_write(cfg: Config, authorization: str | None, client_ip: str) -> tuple[bool, HTTPStatus, str]:
    """(allowed, status, reason). Fails closed when no token is configured."""
    expected = read_token(cfg.token_file)
    if expected is None:
        return False, HTTPStatus.SERVICE_UNAVAILABLE, "writes are disabled: no token file on the host"
    nets = cfg.write_networks()
    if nets:
        try:
            addr = ipaddress.ip_address(client_ip)
        except ValueError:
            addr = None
        if addr is None or not any(addr in n for n in nets):
            return False, HTTPStatus.FORBIDDEN, f"writes are not allowed from {client_ip}"
    if not authorization or not authorization.startswith("Bearer "):
        return False, HTTPStatus.UNAUTHORIZED, "writes need Authorization: Bearer <token>"
    given = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return False, HTTPStatus.FORBIDDEN, "wrong token"
    return True, HTTPStatus.OK, ""


def host_names(cfg: Config) -> set[str]:
    """Host header names accepted besides IP literals (DNS-rebinding guard)."""
    names = {"localhost"}
    for name in (socket.gethostname(), socket.getfqdn()):
        if name:
            names.add(name.lower())
    names.update(h.lower() for h in cfg.allowed_hosts)
    return names


def host_ok(host_header: str | None, names: set[str]) -> bool:
    if not host_header:
        return True  # HTTP/1.0 without Host: not a browser, nothing to rebind
    host = host_header.strip().lower()
    if host.startswith("["):  # [IPv6]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host.rstrip(".") in names


# ---- HTTP ------------------------------------------------------------------
def status_page(st: dict) -> str:
    def v(x, unit=""):
        if x is None:
            return "—"
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return f"{x:g} {unit}".strip()
        return str(x)
    ident = st["identity"] or {}
    e = st["energy"]
    rows = [
        ("Output", "ON" if st["output"] else ("OFF" if st["output"] is False else "—")),
        ("Mode", st["mode"]),
        ("Measured", f"{v(st['measured']['voltage'], 'V')} · {v(st['measured']['current'], 'A')} · {v(st['measured']['power'], 'W')}"),
        ("Setpoints", f"{v(st['setpoints']['voltage'], 'V')} · {v(st['setpoints']['current'], 'A')}"),
        ("OVP / OCP", f"{v(st['protection']['ovp_limit'], 'V')} · {v(st['protection']['ocp_limit'], 'A')}"),
        ("Tripped", ", ".join(k[:3].upper() for k in ("ovp_tripped", "ocp_tripped", "otp_tripped")
                              if st["protection"][k]) or "none"),
        ("Energy", f"{e['wh']:.3f} Wh · {e['ah']:.3f} Ah since {e['since']}"),
        ("Host ceilings", f"{st['ceilings']['max_voltage']:g} V · {st['ceilings']['max_current']:g} A"),
        ("Link", f"{st['link']['state']} · {st['link']['poll_hz']} Hz · age {v(st['link']['age_s'], 's')}"),
        ("Identity", f"{ident.get('manufacturer', '—')} {ident.get('model', '')} #{ident.get('serial', '')} fw {ident.get('firmware', '')}"),
        ("Daemon", f"bench-psud {st['daemon']['version']} since {st['daemon']['started']}"),
    ]
    body = "\n".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(val))}</td></tr>" for k, val in rows)
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in st["warnings"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2"><title>Bench PSU</title>
<style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--warn:#a40}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--warn:#f96}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif;margin:0;padding:16px;max-width:640px}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:500;width:9em}}ul{{color:var(--warn)}}small{{color:var(--mut)}}
</style></head><body>
<h1>Bench PSU</h1>
<table>{body}</table>
<ul>{warn}</ul>
<small>Read-only page. JSON at <a href="/api/status">/api/status</a>. Writes: the <code>kiprim</code> CLI, the API with a token, or MCP at /mcp.</small>
</body></html>"""


MAX_BODY = 65536


def make_handler(d: Daemon):
    names = host_names(d.cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"bench-psud/{__version__}"
        protocol_version = "HTTP/1.1"
        timeout = d.cfg.request_timeout  # socket timeout: a stalled client cannot hold a thread

        def log_message(self, fmt, *args):  # quiet; writes are logged as events
            pass

        def _send(self, status, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else (
                payload.encode() if isinstance(payload, str) else json.dumps(payload, default=str).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for k, val in (extra or {}).items():
                self.send_header(k, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            """Read the whole body before any reply, so a keep-alive connection
            stays in step. None (and the connection marked to close) when the
            length is unusable."""
            raw_len = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return None
            try:
                length = int(raw_len) if raw_len else 0
            except ValueError:
                self.close_connection = True
                return None
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return None
            return self.rfile.read(length) if length else b""

        def _guard(self) -> bool:
            """DNS-rebinding guard for every request: the Host header must be an
            IP literal or one of this host's names, and a browser Origin must
            match the Host. False after sending 403."""
            if not host_ok(self.headers.get("Host"), names):
                self._send(403, {"ok": False, "error": "unknown Host header; add it to [http] allowed_hosts"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1].lower() != (self.headers.get("Host") or "").lower():
                self._send(403, {"ok": False, "error": "cross-origin request refused"})
                return False
            return True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                return self._send(200, d.status())
            if path == "/api/health":
                st = d.status()
                return self._send(200 if st["link"]["ok"] else 503,
                                  {"ok": st["link"]["ok"], "link": st["link"]["state"],
                                   "age_s": st["link"]["age_s"]})
            if path in ("/", "/index.html"):
                return self._send(200, status_page(d.status()), "text/html")
            if path == "/mcp":
                return self._send(405, {"error": "this MCP server has no SSE stream; POST JSON-RPC"},
                                  extra={"Allow": "POST"})
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            raw = self._read_body()  # always first
            if raw is None:
                return self._send(400, {"ok": False, "error": "bad or missing Content-Length"})
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            client = self.client_address[0]
            try:
                body = json.loads(raw) if raw else {}
                if not isinstance(body, (dict, list)):
                    raise ValueError("request body must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"ok": False, "error": f"bad JSON: {exc}"})
            if path == "/mcp":
                if not d.cfg.mcp:
                    return self._send(404, {"error": "MCP is disabled in the config"})
                authorization = self.headers.get("Authorization")
                writer = False
                if authorization:  # a token was offered: it must be right
                    writer, status, reason = check_write(d.cfg, authorization, client)
                    if not writer:
                        log_event("write_refused", path=path, source=client, status=int(status), reason=reason)
                        return self._send(status, {"error": reason})
                response = mcp.handle(d, body, writer=writer, source=f"mcp:{client}")
                if response is None:
                    return self._send(202, b"", "application/json")
                return self._send(200, response)
            op = HTTP_ROUTES.get(path)
            if op is None and path not in ("/api/probe", "/api/counters/reset"):
                return self._send(404, {"ok": False, "error": "not found"})
            allowed, status, reason = check_write(d.cfg, self.headers.get("Authorization"), client)
            if not allowed:
                log_event("write_refused", path=path, source=client, status=int(status), reason=reason)
                return self._send(status, {"ok": False, "error": reason},
                                  extra={"WWW-Authenticate": "Bearer"} if status == 401 else None)
            if not isinstance(body, dict):
                return self._send(400, {"ok": False, "error": "request body must be a JSON object"})
            try:
                if path == "/api/probe":
                    rows = d.run_probe(str(body.get("level", "queries")), f"http:{client}")
                    return self._send(200, {"ok": True, "rows": rows})
                if path == "/api/counters/reset":
                    return self._send(200, {"ok": True, "result": d.run_op("reset_counters", {}, f"http:{client}")})
                result = d.run_op(op, body, f"http:{client}")
            except Exception as exc:
                status, kind = classify(exc)
                payload = {"ok": False, "kind": kind, "error": str(exc), "status": d.status()}
                if isinstance(exc, probe.ProbeAborted):
                    payload["rows"] = probe.rows_as_dicts(exc.rows)
                return self._send(status, payload)
            return self._send(200, {"ok": True, "result": result, "status": d.status()})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(d: Daemon, listen: str, port: int) -> Server:
    return Server((listen, port), make_handler(d))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench-psud", description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None, help="TOML config (default /etc/bench-psu/config.toml)")
    ap.add_argument("--simulate", action="store_true", help="drive a simulated supply instead of the tty")
    ap.add_argument("--listen", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    cfg = load(args.config)
    if args.listen:
        cfg.listen = args.listen
    if args.port:
        cfg.port = args.port
    factory = None
    if args.simulate:
        from .simulator import SimPort, SimulatedSupply
        supply = SimulatedSupply(realistic_timing=True)
        factory = lambda: SimPort(supply)  # noqa: E731
    d = Daemon(cfg, factory)
    server = serve(d, cfg.listen, cfg.port)
    done = threading.Event()
    received = []

    def on_signal(signum, _frame):
        # No I/O here: a print inside a signal handler can re-enter stdout.
        received.append(signum)
        done.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    d.start()
    http_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    http_thread.start()
    log_event("listening", listen=cfg.listen, port=cfg.port, mcp=cfg.mcp)
    exit_code = 0
    while not done.wait(1.0):
        # Watchdog: if a thread died anyway, exit non-zero so systemd restarts
        # us (a restart sends queries only, the output is untouched).
        if not d.worker.is_alive() or not http_thread.is_alive():
            log_event("fatal", worker_alive=d.worker.is_alive(), http_alive=http_thread.is_alive())
            exit_code = 1
            break
    if received:
        log_event("signal", signal=signal.Signals(received[0]).name)
    server.shutdown()
    d.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
