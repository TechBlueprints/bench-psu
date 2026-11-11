# SPDX-License-Identifier: Apache-2.0
import os
import threading

import pytest

from kiprim.simulator import SimPort, SimulatedSupply
from kiprim.transport import (DeviceError, LinkLost, PortBusy, ReplyTimeout, Transport,
                              TransportError, TtyPort)


def test_err_raises(transport):
    with pytest.raises(DeviceError):
        transport.query("SYST:STAT?")
    with pytest.raises(DeviceError):
        transport.write("BOGUS 1")


def test_silent_setter_is_accepted(transport, supply):
    transport.write("VOLT 12.00")
    assert supply.voltage == 12.0


def test_lost_after_consecutive_timeouts(supply, transport):
    supply.drop_replies = 10
    for _ in range(2):
        with pytest.raises(ReplyTimeout):
            transport.query("VOLT?")
    with pytest.raises(LinkLost):
        transport.query("VOLT?")
    assert transport.lost
    with pytest.raises(LinkLost):
        transport.query("*IDN?")  # refuses traffic until reopened


def test_timeouts_reset_on_success(supply, transport):
    supply.drop_replies = 2
    for _ in range(2):
        with pytest.raises(ReplyTimeout):
            transport.query("VOLT?")
    assert transport.query("VOLT?") == "10.000"
    assert transport.consecutive_timeouts == 0


def test_stale_input_flushed(supply):
    port = SimPort(supply)
    t = Transport(port, 0.05, 0.01)
    port._pending.append(b"stale")
    assert t.query("VOLT?") == "10.000"


def test_multiline_command_refused(transport):
    with pytest.raises(ValueError):
        transport.query("VOLT?\nOUTP ON")


# ---- the real termios path, over a pty pair ----------------------------------
def _pty():
    """A pty pair, opened only by its path afterwards. The slave fd from
    openpty() is closed at once: on Linux TIOCEXCL stays set until the last
    fd on the tty closes, so a stray fd would keep the port "busy"."""
    master, slave = os.openpty()
    name = os.ttyname(slave)
    os.close(slave)
    return master, None, name


def test_ttyport_roundtrip_over_pty():
    master, slave, name = _pty()
    try:
        port = TtyPort(name)
        t = Transport(port, reply_timeout=1.0, setter_error_window=0.05)
        supply = SimulatedSupply()

        def responder():
            buf = b""
            while b"\n" not in buf:
                buf += os.read(master, 64)
            reply = supply.handle(buf.split(b"\n")[0].decode())
            os.write(master, reply.encode() + b"\r\n")

        th = threading.Thread(target=responder)
        th.start()
        assert t.query("*IDN?") == "KIPRIM,DC620S,SIM00001,FV:V2.3.0"
        th.join(2)
        port.close()
    finally:
        os.close(master)


def test_ttyport_exclusive():
    master, slave, name = _pty()
    try:
        first = TtyPort(name)
        with pytest.raises(PortBusy):
            TtyPort(name)
        first.close()
        TtyPort(name).close()  # lock released on close
    finally:
        os.close(master)


def test_ttyport_missing_device():
    with pytest.raises(TransportError):
        TtyPort("/dev/does-not-exist-kiprim")
