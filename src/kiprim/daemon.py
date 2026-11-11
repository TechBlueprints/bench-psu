# SPDX-License-Identifier: Apache-2.0
"""``bench-psud``: the only process that talks to the supply.

One worker thread owns the serial port. It polls the supply a few times a
second, keeps the latest state, and runs write jobs queued by the HTTP API
and the MCP endpoint, one at a time, between polls.

Starting, stopping, restarting or reconnecting sends **queries only**. The
output changes only when a client asks for it, or on exit when
``output_off_on_exit`` is set in the config (default off).

Every state change, write and refused write is one JSON line on stdout, which
systemd puts in the journal.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, mcp, parse, probe
from .config import Config, load
from .control import ConflictError, Controller, CycleError, LimitError
from .instrument import Instrument, ReadbackMismatch
from .transport import (
    DeviceError, LinkLost, PortBusy, ReplyTimeout, Transport, TransportError, TtyPort,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log_event(event: str, **fields) -> None:
    record = {"ts": now_iso(), "event": event, **fields}
    print(json.dumps(record, separators=(",", ":"), default=str), flush=True)


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


# ---- operations shared by the HTTP API and MCP ---------------------------
# name -> (handler(controller, args) -> dict, JSON schema of args, description)
OPS = {
    "set_voltage": (
        lambda c, a: c.set_voltage(a.get("volts")),
        {"type": "object", "properties": {"volts": {"type": "number", "minimum": 0}},
         "required": ["volts"]},
        "Set the voltage setpoint (applies live; refused above the host ceiling or the OVP limit).",
    ),
    "set_current": (
        lambda c, a: c.set_current(a.get("amps"), live=bool(a.get("live", False))),
        {"type": "object", "properties": {
            "amps": {"type": "number", "minimum": 0},
            "live": {"type": "boolean", "default": False,
                     "description": "Write CURR with the output on instead of cycling it off/on."}},
         "required": ["amps"]},
        "Set the current limit. If the output is on it is switched off, the limit set, and "
        "switched back on (the firmware may trip OCP on a live write) unless live=true.",
    ),
    "set_voltage_current": (
        lambda c, a: c.set_both(a.get("volts"), a.get("amps"), live=bool(a.get("live", False))),
        {"type": "object", "properties": {
            "volts": {"type": "number", "minimum": 0}, "amps": {"type": "number", "minimum": 0},
            "live": {"type": "boolean", "default": False}},
         "required": ["volts", "amps"]},
        "Set voltage and current together, with at most one output off/on cycle.",
    ),
    "set_output": (
        lambda c, a: c.set_output(a.get("on")),
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        "Switch the output on or off. Switching on is refused while a setpoint is above a host ceiling.",
    ),
    "set_ovp": (
        lambda c, a: c.set_ovp(a.get("volts")),
        {"type": "object", "properties": {"volts": {"type": "number", "minimum": 0}},
         "required": ["volts"]},
        "Set the over-voltage protection limit (never below the voltage setpoint).",
    ),
    "set_ocp": (
        lambda c, a: c.set_ocp(a.get("amps")),
        {"type": "object", "properties": {"amps": {"type": "number", "minimum": 0}},
         "required": ["amps"]},
        "Set the over-current protection limit (never below the current setpoint).",
    ),
    "set_lock": (
        lambda c, a: c.set_lock(a.get("locked")),
        {"type": "object", "properties": {"locked": {"type": "boolean"}}, "required": ["locked"]},
        "Lock or unlock the front panel.",
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
    (CycleError, HTTPStatus.BAD_GATEWAY, "cycle-failed"),
    (ReadbackMismatch, HTTPStatus.BAD_GATEWAY, "readback-mismatch"),
    (DeviceError, HTTPStatus.BAD_GATEWAY, "device-error"),
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
            "front_panel_lock": None,
        }
        self.link = {"state": "connecting", "device": cfg.device, "last_poll": None,
                     "last_poll_mono": None, "consecutive_timeouts": 0, "reconnects": 0,
                     "last_error": None, "connected_since": None}
        self._poll_count = 0
        self._poll_window = (time.monotonic(), 0)
        self.poll_hz = 0.0
        self.worker = threading.Thread(target=self._worker, name="psu-worker", daemon=True)

    # ---- public ----------------------------------------------------------
    def start(self) -> None:
        log_event("start", version=__version__, device=self.cfg.device,
                  max_voltage=self.cfg.ceilings.max_voltage, max_current=self.cfg.ceilings.max_current,
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
        rows = self.submit("probe", lambda: probe.rows_as_dicts(probe.run(self.inst, level, self.cfg.ceilings)),
                           timeout=60.0)
        log_event("probe_done", level=level, rows=len(rows),
                  results={r["command"]: r["result"] for r in rows})
        return rows

    def status(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self.state))
            link = dict(self.link)
        mono = link.pop("last_poll_mono")
        link["age_s"] = None if mono is None else round(time.monotonic() - mono, 3)
        link["poll_hz"] = round(self.poll_hz, 2)
        link["ok"] = link["state"] == "ok"
        warnings = []
        sp = st["setpoints"]
        c = self.cfg.ceilings
        if sp["voltage"] is not None and sp["voltage"] > c.max_voltage:
            warnings.append(f"voltage setpoint {sp['voltage']} V is above the host ceiling {c.max_voltage} V")
        if sp["current"] is not None and sp["current"] > c.max_current:
            warnings.append(f"current setpoint {sp['current']} A is above the host ceiling {c.max_current} A")
        if link["state"] != "ok":
            warnings.append(f"link {link['state']}: {link['last_error'] or 'no data yet'}")
        if any(st["protection"][k] for k in ("ovp_tripped", "ocp_tripped", "otp_tripped")):
            warnings.append("a protection has tripped")
        return {
            "time": now_iso(),
            **st,
            "link": link,
            "ceilings": {"max_voltage": c.max_voltage, "max_current": c.max_current},
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
        except TransportError as exc:
            self._set_link("absent", str(exc))
            return False
        t = Transport(port, self.cfg.reply_timeout, self.cfg.setter_error_window, self.cfg.lost_after)
        inst = Instrument(t)
        try:
            ident = inst.idn()
        except (TransportError, parse.ParseError) as exc:
            t.close()
            self._set_link("no-reply", f"*IDN? failed: {exc}")
            return False
        self.inst, self.ctl = inst, Controller(inst, self.cfg.ceilings)
        with self._lock:
            self.state["identity"] = ident.as_dict()
            self.link["consecutive_timeouts"] = 0
            self.link["connected_since"] = now_iso()
            self.link["reconnects"] += 1
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
            if self.inst is None:
                if not self._connect():
                    self._drain_jobs(NotConnected(f"supply not connected ({self.link['state']})"))
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
            next_poll = time.monotonic() + self.cfg.poll_interval
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
        changes = []
        with self._lock:
            st = self.state
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
            self.link["last_poll_mono"] = time.monotonic()
            self.link["consecutive_timeouts"] = 0
            start, count = self._poll_window
            count += 1
            elapsed = time.monotonic() - start
            if elapsed >= 5.0:
                self.poll_hz = count / elapsed
                self._poll_window = (time.monotonic(), 0)
            else:
                self._poll_window = (start, count)
        if self.link["state"] != "ok":
            self._set_link("ok")
        for event, fields in changes:
            log_event(event, **fields)

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


# ---- HTTP ------------------------------------------------------------------
def status_page(st: dict) -> str:
    def v(x, unit=""):
        return "—" if x is None else f"{x:g} {unit}".strip() if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x)
    ident = st["identity"] or {}
    rows = [
        ("Output", "ON" if st["output"] else ("OFF" if st["output"] is False else "—")),
        ("Mode", st["mode"]),
        ("Measured", f"{v(st['measured']['voltage'], 'V')} · {v(st['measured']['current'], 'A')} · {v(st['measured']['power'], 'W')}"),
        ("Setpoints", f"{v(st['setpoints']['voltage'], 'V')} · {v(st['setpoints']['current'], 'A')}"),
        ("OVP / OCP", f"{v(st['protection']['ovp_limit'], 'V')} · {v(st['protection']['ocp_limit'], 'A')}"),
        ("Tripped", ", ".join(k[:3].upper() for k in ("ovp_tripped", "ocp_tripped", "otp_tripped")
                              if st["protection"][k]) or "none"),
        ("Host ceilings", f"{st['ceilings']['max_voltage']:g} V · {st['ceilings']['max_current']:g} A"),
        ("Link", f"{st['link']['state']} · {st['link']['poll_hz']} Hz · age {v(st['link']['age_s'], 's')}"),
        ("Identity", f"{ident.get('manufacturer', '—')} {ident.get('model', '')} #{ident.get('serial', '')} fw {ident.get('firmware', '')}"),
        ("Daemon", f"bench-psud {st['daemon']['version']} since {st['daemon']['started']}"),
    ]
    import html
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


def make_handler(d: Daemon):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"bench-psud/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; writes are logged as events
            pass

        def _send(self, status, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else (
                payload.encode() if isinstance(payload, str) else json.dumps(payload, default=str).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, val in (extra or {}).items():
                self.send_header(k, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _body(self) -> dict | list:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            data = json.loads(raw)
            if not isinstance(data, (dict, list)):
                raise ValueError("request body must be a JSON object")
            return data

        def _origin_ok(self) -> bool:
            # DNS-rebinding guard: a browser page from another site may not drive us.
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return origin.split("://", 1)[-1] == host

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                return self._send(200, d.status())
            if path == "/api/health":
                st = d.status()
                return self._send(200 if st["link"]["ok"] else 503,
                                  {"ok": st["link"]["ok"], "link": st["link"]["state"]})
            if path in ("/", "/index.html"):
                return self._send(200, status_page(d.status()), "text/html")
            if path == "/mcp":
                return self._send(405, {"error": "this MCP server has no SSE stream; POST JSON-RPC"},
                                  extra={"Allow": "POST"})
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            client = self.client_address[0]
            if not self._origin_ok():
                return self._send(403, {"ok": False, "error": "cross-origin request refused"})
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"ok": False, "error": f"bad JSON: {exc}"})
            if path == "/mcp":
                allowed, _, _ = check_write(d.cfg, self.headers.get("Authorization"), client)
                if not d.cfg.mcp:
                    return self._send(404, {"error": "MCP is disabled in the config"})
                response = mcp.handle(d, body, writer=allowed, source=f"mcp:{client}")
                if response is None:
                    return self._send(202, b"", "application/json")
                return self._send(200, response)
            op = HTTP_ROUTES.get(path)
            if op is None and path != "/api/probe":
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
                result = d.run_op(op, body, f"http:{client}")
            except Exception as exc:
                status, kind = classify(exc)
                return self._send(status, {"ok": False, "kind": kind, "error": str(exc),
                                           "status": d.status()})
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
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    log_event("listening", listen=cfg.listen, port=cfg.port, mcp=cfg.mcp)
    done.wait()
    log_event("signal", signal=signal.Signals(received[0]).name if received else None)
    server.shutdown()
    d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
