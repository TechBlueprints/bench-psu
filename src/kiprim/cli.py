# SPDX-License-Identifier: Apache-2.0
"""``kiprim``: command line for the bench supply.

Talks to ``bench-psud`` over HTTP (default ``http://127.0.0.1:<port>`` from
the host config). Writes send the bearer token read from the token file named
in the config, or ``$KIPRIM_TOKEN``. ``--direct`` opens the tty itself, which
only works while the daemon is stopped (the port lock refuses otherwise).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import __version__, probe
from .config import load

EXIT_ERROR, EXIT_REFUSED = 1, 2


class CliError(Exception):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


# ---- backends --------------------------------------------------------------
class DaemonBackend:
    def __init__(self, url: str, token: str | None, timeout: float = 70.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token and method == "POST":
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except Exception:
                payload = {"error": exc.reason}
            raise CliError(f"HTTP {exc.code}: {payload.get('error', payload)}", payload) from None
        except urllib.error.URLError as exc:
            raise CliError(f"cannot reach bench-psud at {self.url}: {exc.reason} "
                           "(is the service running? try --direct if it is stopped)") from None

    def status(self) -> dict:
        return self._req("GET", "/api/status")

    def op(self, path: str, body: dict) -> dict:
        if not self.token:
            raise CliError("no token: run on the host as root (token file) or set KIPRIM_TOKEN")
        return self._req("POST", path, body)["result"]

    def probe(self, level: str) -> list[dict]:
        if not self.token:
            raise CliError("probe goes through the daemon and needs the token (or use --direct)")
        return self._req("POST", "/api/probe", {"level": level})["rows"]


class DirectBackend:
    def __init__(self, cfg):
        from .control import Controller
        from .instrument import Instrument, check_identity
        from .transport import Transport, TtyPort

        self.cfg = cfg
        port = TtyPort(cfg.device)
        self.inst = Instrument(Transport(port, cfg.reply_timeout, cfg.setter_error_window, cfg.lost_after))
        try:
            check_identity(self.inst.idn(), cfg.expect_idn_prefix, cfg.expect_serial)
        except BaseException:
            self.inst.t.close()
            raise
        self.ctl = Controller(self.inst, cfg.ceilings)

    def status(self) -> dict:
        i = self.inst
        ident = i.idn()
        info = i.measure_info()
        sp = i.setpoints()
        if info is None:
            ma = i.measure_all()
            measured = {"voltage": ma.voltage, "current": ma.current,
                        "power": ma.power if ma.power is not None else i.measure_power()}
            flags = {"ovp_tripped": None, "ocp_tripped": None, "otp_tripped": None}
            mode = "unknown"
        else:
            measured = {"voltage": info.voltage, "current": info.current, "power": info.power}
            flags = {"ovp_tripped": info.ovp_tripped, "ocp_tripped": info.ocp_tripped,
                     "otp_tripped": info.otp_tripped}
            mode = info.mode
        return {
            "identity": ident.as_dict(), "output": i.output(), "mode": mode,
            "setpoints": {"voltage": sp.voltage, "current": sp.current},
            "protection": {"ovp_limit": sp.ovp, "ocp_limit": sp.ocp, **flags},
            "measured": measured,
            "ceilings": {"max_voltage": self.cfg.ceilings.max_voltage,
                         "max_current": self.cfg.ceilings.max_current,
                         "ovp_margin": self.cfg.ceilings.ovp_margin,
                         "ocp_margin": self.cfg.ceilings.ocp_margin},
            "link": {"state": "direct", "device": self.cfg.device},
            "warnings": [],
        }

    _OPS = {
        "/api/voltage": lambda c, b: c.set_voltage(b["volts"]),
        "/api/current": lambda c, b: c.set_current(b["amps"], live=b.get("live", False)),
        "/api/set": lambda c, b: c.set_both(b["volts"], b["amps"], live=b.get("live", False)),
        "/api/output": lambda c, b: c.set_output(b["on"]),
        "/api/ovp": lambda c, b: c.set_ovp(b["volts"], live=b.get("live", False)),
        "/api/ocp": lambda c, b: c.set_ocp(b["amps"], live=b.get("live", False)),
        "/api/lock": lambda c, b: c.set_lock(b["locked"]),
    }

    def op(self, path: str, body: dict) -> dict:
        if path == "/api/counters/reset":
            raise CliError("energy counters live in the daemon; there are none in --direct mode")
        return self._OPS[path](self.ctl, body)

    def probe(self, level: str) -> list[dict]:
        return probe.rows_as_dicts(probe.run(self.inst, level, self.cfg.ceilings))


# ---- output ----------------------------------------------------------------
def _n(x, unit):
    return "—" if x is None else f"{x:.3f} {unit}"


def format_status(st: dict) -> str:
    ident = st.get("identity") or {}
    p = st["protection"]
    tripped = [k[:3].upper() for k in ("ovp_tripped", "ocp_tripped", "otp_tripped") if p.get(k)]
    out = "ON" if st["output"] else ("OFF" if st["output"] is False else "?")
    lines = [
        f"{ident.get('manufacturer', '?')} {ident.get('model', '')} #{ident.get('serial', '')} "
        f"fw {ident.get('firmware', '')}  link {st['link']['state']}",
        f"output   {out:<4} mode {st['mode']}",
        f"measured {_n(st['measured']['voltage'], 'V')}  {_n(st['measured']['current'], 'A')}  "
        f"{_n(st['measured']['power'], 'W')}",
        f"setpoint {_n(st['setpoints']['voltage'], 'V')}  {_n(st['setpoints']['current'], 'A')}",
        f"limits   OVP {_n(p['ovp_limit'], 'V')}  OCP {_n(p['ocp_limit'], 'A')}  "
        f"tripped: {', '.join(tripped) or 'none'}",
        f"ceilings {st['ceilings']['max_voltage']:g} V  {st['ceilings']['max_current']:g} A (host)",
    ]
    if st.get("energy"):
        e = st["energy"]
        lines.append(f"energy   {e['wh']:.3f} Wh  {e['ah']:.3f} Ah  on {e['output_on_s']:.0f} s  since {e['since']}")
    lines += [f"warning: {w}" for w in st.get("warnings", [])]
    return "\n".join(lines)


def format_probe(rows: list[dict]) -> str:
    w = max(len(r["command"]) for r in rows)
    out = []
    for r in rows:
        ms = "" if r.get("ms") is None else f"{r['ms']:>6.1f} ms"
        out.append(f"{r['command']:<{w}}  {r['kind']:<6}  {r['result']:<10}  {ms:>9}  "
                   f"{r['reply']}{'  — ' + r['note'] if r['note'] else ''}")
    return "\n".join(out)


# ---- main ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kiprim", description="Control the bench power supply.")
    ap.add_argument("--version", action="version", version=f"kiprim {__version__}")
    ap.add_argument("--config", default=None, help="config file (default /etc/bench-psu/config.toml)")
    ap.add_argument("--url", default=os.environ.get("KIPRIM_URL"), help="daemon URL")
    ap.add_argument("--token-file", default=None, help="file holding the bearer token")
    ap.add_argument("--direct", action="store_true", help="open the tty directly (daemon must be stopped)")
    ap.add_argument("--device", default=None, help="tty for --direct (default: the config's device)")
    ap.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("idn", help="identity")
    sub.add_parser("status", help="full state")
    s = sub.add_parser("set", help="set voltage and current limit")
    s.add_argument("volts", type=float)
    s.add_argument("amps", type=float)
    s.add_argument("--live", action="store_true", help="do not cycle the output for a current change")
    s = sub.add_parser("volt", help="set voltage only")
    s.add_argument("volts", type=float)
    s = sub.add_parser("curr", help="set current limit only")
    s.add_argument("amps", type=float)
    s.add_argument("--live", action="store_true", help="do not cycle the output")
    sub.add_parser("on", help="output on")
    sub.add_parser("off", help="output off")
    s = sub.add_parser("ovp", help="set over-voltage protection limit")
    s.add_argument("volts", type=float)
    s.add_argument("--live", action="store_true", help="do not cycle the output")
    s = sub.add_parser("ocp", help="set over-current protection limit")
    s.add_argument("amps", type=float)
    s.add_argument("--live", action="store_true", help="do not cycle the output")
    sub.add_parser("reset-counters", help="reset the daemon's Wh/Ah counters")
    sub.add_parser("lock", help="lock the front panel")
    sub.add_parser("unlock", help="unlock the front panel")
    s = sub.add_parser("watch", help="print measurements until Ctrl-C")
    s.add_argument("-i", "--interval", type=float, default=1.0)
    s = sub.add_parser("probe", help="run the protocol table and print a conformance table")
    s.add_argument("--level", choices=probe.LEVELS, default="queries",
                   help="queries (default, changes nothing) < setters < output < ocp_quirk")
    s.add_argument("--markdown", action="store_true", help="print a Markdown table")
    return ap


def resolve_token(args, cfg) -> str | None:
    if os.environ.get("KIPRIM_TOKEN"):
        return os.environ["KIPRIM_TOKEN"].strip()
    path = args.token_file or cfg.token_file
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.config)
    except PermissionError:
        if args.config:
            raise
        from .config import Config
        cfg = Config()  # not root/bench-psu: defaults are enough for reads
    except Exception as exc:
        print(f"kiprim: config: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.direct:
            if args.device:
                cfg.device = args.device
            backend = DirectBackend(cfg)
        else:
            url = args.url or f"http://127.0.0.1:{cfg.port}"
            backend = DaemonBackend(url, resolve_token(args, cfg))
        return run(args, backend)
    except CliError as exc:
        print(f"kiprim: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # LimitError, ConflictError, transport errors in --direct
        print(f"kiprim: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def run(args, backend) -> int:
    def show(obj, text=None):
        print(json.dumps(obj, indent=1) if args.json or text is None else text)

    c = args.cmd
    if c == "idn":
        st = backend.status()
        ident = st.get("identity") or {}
        show(ident, ident.get("raw", "?"))
    elif c == "status":
        st = backend.status()
        show(st, format_status(st))
    elif c in ("set", "volt", "curr", "on", "off", "ovp", "ocp", "lock", "unlock", "reset-counters"):
        path, body = {
            "set": lambda: ("/api/set", {"volts": args.volts, "amps": args.amps, "live": args.live}),
            "volt": lambda: ("/api/voltage", {"volts": args.volts}),
            "curr": lambda: ("/api/current", {"amps": args.amps, "live": args.live}),
            "on": lambda: ("/api/output", {"on": True}),
            "off": lambda: ("/api/output", {"on": False}),
            "ovp": lambda: ("/api/ovp", {"volts": args.volts, "live": args.live}),
            "ocp": lambda: ("/api/ocp", {"amps": args.amps, "live": args.live}),
            "reset-counters": lambda: ("/api/counters/reset", {}),
            "lock": lambda: ("/api/lock", {"locked": True}),
            "unlock": lambda: ("/api/lock", {"locked": False}),
        }[c]()
        result = backend.op(path, body)
        show(result, " ".join(f"{k}={v}" for k, v in result.items()))
    elif c == "watch":
        while True:
            st = backend.status()
            m = st["measured"]
            print(f"{time.strftime('%H:%M:%S')}  {'ON ' if st['output'] else 'OFF'}  "
                  f"{st['mode']:<7}  {_n(m['voltage'], 'V')}  {_n(m['current'], 'A')}  "
                  f"{_n(m['power'], 'W')}", flush=True)
            time.sleep(args.interval)
    elif c == "probe":
        try:
            rows = backend.probe(args.level)
        except CliError as exc:
            if exc.payload.get("rows"):
                print(format_probe(exc.payload["rows"]))
            raise
        except probe.ProbeAborted as exc:
            print(format_probe(probe.rows_as_dicts(exc.rows)))
            raise
        if args.json:
            show(rows)
        elif args.markdown:
            print(probe.markdown(rows))
        else:
            print(format_probe(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
