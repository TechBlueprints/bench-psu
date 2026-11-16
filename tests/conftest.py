# SPDX-License-Identifier: Apache-2.0
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from kiprim.config import Config
from kiprim.control import Ceilings, Controller
from kiprim.daemon import Daemon, serve
from kiprim.instrument import Instrument
from kiprim.simulator import SimPort, SimulatedSupply
from kiprim.transport import Transport

TOKEN = "test-token-not-secret"


@pytest.fixture
def events(monkeypatch):
    """The daemon's log events, recorded where they are made.

    Reading a worker thread's line back through capsys raced on Linux CI with
    Python 3.13 (the line reached the real stdout), so tests that check what
    the daemon logged use this list instead. The lines still go to stdout.
    """
    import kiprim.daemon as daemon_mod
    seen = []
    lock = threading.Lock()
    real = daemon_mod.log_event

    def record(event, **fields):
        with lock:
            seen.append({"event": event, **fields})
        real(event, **fields)

    monkeypatch.setattr(daemon_mod, "log_event", record)
    return seen


@pytest.fixture
def supply():
    return SimulatedSupply()


@pytest.fixture
def transport(supply):
    return Transport(SimPort(supply), reply_timeout=0.05, setter_error_window=0.01, lost_after=3)


@pytest.fixture
def inst(transport):
    return Instrument(transport)


@pytest.fixture
def ctl(inst):
    return Controller(inst, Ceilings(15.0, 10.0))


def wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Running:
    def __init__(self, d, server, supply, base):
        self.d, self.server, self.supply, self.base = d, server, supply, base

    def req(self, method, path, body=None, token=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw and "json" in resp.headers.get("Content-Type", "") else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, raw

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.d.stop()


@pytest.fixture
def make_daemon(tmp_path):
    running = []

    def _make(supply=None, token=TOKEN, **cfg_kw):
        supply = supply or SimulatedSupply()
        token_file = tmp_path / "token"
        if token is not None:
            token_file.write_text(token + "\n")
        kw = dict(device="sim", reply_timeout=0.05, setter_error_window=0.01,
                  poll_interval=0.05, setpoint_interval=0.2, reconnect_interval=0.05,
                  listen="127.0.0.1", port=0, token_file=str(token_file))
        kw.update(cfg_kw)
        cfg = Config(**kw)
        d = Daemon(cfg, lambda: SimPort(supply))
        server = serve(d, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        d.start()
        assert wait_for(lambda: d.status()["link"]["state"] == "ok"), d.status()
        r = Running(d, server, supply, f"http://127.0.0.1:{server.server_address[1]}")
        running.append(r)
        return r

    yield _make
    for r in running:
        try:
            r.stop()
        except Exception:
            pass
