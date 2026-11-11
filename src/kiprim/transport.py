# SPDX-License-Identifier: Apache-2.0
"""Line transport for the supply's USB serial port.

Standard library only (``termios``, ``fcntl``, ``select``), so the host needs
no extra package. The port is opened by path (use a stable udev symlink, not
``/dev/ttyUSB0``), locked with an exclusive ``flock`` so only one process can
drive the supply, and also marked ``TIOCEXCL`` so non-root processes that do
not use ``flock`` cannot open it either.

Framing is one ``\\n``-terminated command out, at most one line back. Setters
answer nothing on success and ``ERR`` on a rejected command; queries answer a
bare value or ``ERR``. After ``lost_after`` consecutive reply timeouts the
transport declares the link lost and refuses further traffic until reopened.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import termios
import threading
import time
from typing import Protocol


class TransportError(Exception):
    """Base class for transport failures."""


class PortBusy(TransportError):
    """Another process holds the port (flock or TIOCEXCL)."""


class DeviceError(TransportError):
    """The supply answered ``ERR``."""

    def __init__(self, command: str):
        super().__init__(f"supply answered ERR to {command!r}")
        self.command = command


class ReplyTimeout(TransportError):
    """A query got no reply within the timeout."""


class LinkLost(TransportError):
    """Too many consecutive timeouts, or the port went away."""


class Port(Protocol):
    def write(self, data: bytes) -> None: ...
    def read_line(self, timeout: float) -> bytes | None: ...
    def flush_input(self) -> None: ...
    def close(self) -> None: ...


class TtyPort:
    """A raw 8N1 tty at 115200 baud with an exclusive lock."""

    def __init__(self, path: str, baud: int = 115200):
        self.path = path
        speed = getattr(termios, f"B{baud}", None)
        if speed is None:
            raise ValueError(f"unsupported baud rate {baud}")
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise PortBusy(f"{path} is held exclusively by another process") from exc
            raise TransportError(f"cannot open {path}: {exc.strerror}") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PortBusy(f"{path} is locked by another process (flock)") from exc
            tiocexcl = getattr(termios, "TIOCEXCL", None)
            if tiocexcl is not None:
                fcntl.ioctl(fd, tiocexcl)
            attrs = termios.tcgetattr(fd)
            iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
            iflag &= ~(
                termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                | termios.INLCR | termios.IGNCR | termios.ICRNL
                | termios.IXON | termios.IXOFF | getattr(termios, "IXANY", 0)
            )
            oflag &= ~termios.OPOST
            lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0))
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except BaseException:
            os.close(fd)
            raise
        self._fd: int | None = fd
        self._buf = b""

    def _require_fd(self) -> int:
        if self._fd is None:
            raise LinkLost(f"{self.path} is closed")
        return self._fd

    def write(self, data: bytes) -> None:
        fd = self._require_fd()
        view = memoryview(data)
        deadline = time.monotonic() + 1.0
        while view:
            try:
                n = os.write(fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise LinkLost(f"write to {self.path} stalled")
                select.select([], [fd], [], 0.05)
            except OSError as exc:
                raise LinkLost(f"write to {self.path} failed: {exc.strerror}") from exc
        try:
            termios.tcdrain(fd)
        except termios.error:
            pass

    def read_line(self, timeout: float) -> bytes | None:
        fd = self._require_fd()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise LinkLost(f"read from {self.path} failed: {exc.strerror}") from exc
            if not chunk:
                # Readable with no data: the device node went away (USB unplug).
                raise LinkLost(f"{self.path} hung up")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def flush_input(self) -> None:
        fd = self._require_fd()
        self._buf = b""
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self) -> None:
        if self._fd is not None:
            try:
                # Clear exclusive mode explicitly: the kernel keeps TIOCEXCL on
                # the tty for as long as anything else holds it open (a pty
                # master, a stray fd), which would lock out our own reopen.
                tiocnxcl = getattr(termios, "TIOCNXCL", None)
                if tiocnxcl is not None:
                    try:
                        fcntl.ioctl(self._fd, tiocnxcl)
                    except OSError:
                        pass
                os.close(self._fd)  # also drops the flock
            finally:
                self._fd = None


class Transport:
    """Command/reply framing, ``ERR`` detection and link-loss tracking.

    Thread-safe: one lock serialises every exchange, but the daemon still keeps
    a single worker thread as the only caller.
    """

    def __init__(
        self,
        port: Port,
        reply_timeout: float = 0.5,
        setter_error_window: float = 0.15,
        lost_after: int = 3,
    ):
        self.port = port
        self.reply_timeout = reply_timeout
        self.setter_error_window = setter_error_window
        self.lost_after = lost_after
        self.consecutive_timeouts = 0
        self.lost = False
        self.last_ok: float | None = None
        self.exchanges = 0
        self._lock = threading.Lock()
        self.on_exchange = None  # optional callback(command, reply_or_None, seconds)

    def _send(self, command: str) -> None:
        if self.lost:
            raise LinkLost("link is lost; reopen the transport")
        if "\n" in command or "\r" in command:
            raise ValueError("command must be a single line")
        self.port.flush_input()  # drop anything stale so replies pair with commands
        try:
            self.port.write(command.encode("ascii") + b"\n")
        except LinkLost:
            self.lost = True
            raise

    def _read(self, timeout: float) -> str | None:
        try:
            raw = self.port.read_line(timeout)
        except LinkLost:
            self.lost = True
            raise
        if raw is None:
            return None
        return raw.decode("ascii", errors="replace").strip()

    def _note(self, command: str, reply: str | None, started: float) -> None:
        self.exchanges += 1
        if self.on_exchange is not None:
            self.on_exchange(command, reply, time.monotonic() - started)

    def query(self, command: str) -> str:
        """Send a query and return its one-line reply; ``ERR`` raises."""
        with self._lock:
            started = time.monotonic()
            self._send(command)
            reply = self._read(self.reply_timeout)
            # Some firmware echoes an empty line first; skip blank lines once.
            if reply == "":
                reply = self._read(self.reply_timeout)
            self._note(command, reply, started)
            if reply is None:
                self.consecutive_timeouts += 1
                if self.consecutive_timeouts >= self.lost_after:
                    self.lost = True
                    raise LinkLost(
                        f"{self.consecutive_timeouts} consecutive timeouts (last: {command!r})"
                    )
                raise ReplyTimeout(f"no reply to {command!r} within {self.reply_timeout}s")
            self.consecutive_timeouts = 0
            self.last_ok = time.time()
            if reply.upper() == "ERR":
                raise DeviceError(command)
            return reply

    def write(self, command: str) -> None:
        """Send a setter. Silence means accepted; ``ERR`` raises.

        Silence is not proof the value took: callers read back and compare.
        """
        with self._lock:
            started = time.monotonic()
            self._send(command)
            reply = self._read(self.setter_error_window)
            self._note(command, reply, started)
            if reply is None or reply == "":
                return
            self.last_ok = time.time()
            if reply.upper() == "ERR":
                raise DeviceError(command)
            raise TransportError(f"unexpected reply {reply!r} to setter {command!r}")

    def close(self) -> None:
        with self._lock:
            self.port.close()
            self.lost = True
