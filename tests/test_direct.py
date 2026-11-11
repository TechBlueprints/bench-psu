# SPDX-License-Identifier: Apache-2.0
"""`kiprim --direct` over a pty with a simulated supply on the other end."""
import os
import threading

import pytest

from kiprim import cli
from kiprim.config import Config
from kiprim.instrument import WrongDevice
from kiprim.simulator import SimulatedSupply


@pytest.fixture
def pty_supply():
    master, slave = os.openpty()
    supply = SimulatedSupply()
    stop = threading.Event()

    def responder():
        buf = b""
        while not stop.is_set():
            try:
                chunk = os.read(master, 256)
            except OSError:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                reply = supply.handle(line.decode())
                if reply is not None:
                    os.write(master, reply.encode() + b"\r\n")

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    yield os.ttyname(slave), supply
    stop.set()
    os.close(slave)
    os.close(master)


def backend(path, **kw):
    return cli.DirectBackend(Config(device=path, reply_timeout=1.0, setter_error_window=0.05, **kw))


def test_direct_status_and_set(pty_supply, capsys):
    path, supply = pty_supply
    b = backend(path)
    st = b.status()
    assert st["identity"]["model"] == "DC620S" and st["link"]["state"] == "direct"
    assert b.op("/api/set", {"volts": 12.0, "amps": 1.0})["voltage"] == 12.0
    assert (supply.voltage, supply.current) == (12.0, 1.0)
    args = cli.build_parser().parse_args(["status"])
    assert cli.run(args, b) == 0 and "setpoint 12.000 V" in capsys.readouterr().out
    b.inst.t.close()


def test_direct_enforces_ceilings(pty_supply):
    path, supply = pty_supply
    b = backend(path)
    with pytest.raises(Exception, match="host ceiling"):
        b.op("/api/voltage", {"volts": 20})
    assert supply.setters_received() == []
    b.inst.t.close()


def test_direct_refuses_wrong_device(pty_supply):
    path, supply = pty_supply
    supply.model = "SPE6103"
    with pytest.raises(WrongDevice):
        backend(path)
    assert supply.log == ["*IDN?"]


def test_direct_refused_while_port_held(pty_supply):
    from kiprim.transport import PortBusy
    path, _ = pty_supply
    first = backend(path)
    with pytest.raises(PortBusy):
        backend(path)
    first.inst.t.close()
