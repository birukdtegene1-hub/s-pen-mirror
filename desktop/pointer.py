"""A session-owned Wayland virtual pointer for authenticated touchpad input."""

import logging
import math
import os
import socket
import struct
import time
from pathlib import Path

from desktop.magnifier import DesktopMagnifier


MANAGER_INTERFACE = b"zwlr_virtual_pointer_manager_v1\0"
DISPLAY, REGISTRY, MANAGER, POINTER = 1, 2, 4, 5
LEFT, RIGHT, MIDDLE = 0x110, 0x111, 0x112
RETRY_DELAY = 5
log = logging.getLogger(__name__)


def decode_pointer(data):
    action = data[:1]
    if action in (b"M", b"S") and len(data) == 9:
        dx, dy = struct.unpack("!ff", data[1:])
        if all(math.isfinite(value) and abs(value) <= 4096 for value in (dx, dy)):
            return action, dx, dy
    elif action == b"C" and len(data) == 2 and 1 <= data[1] <= 3:
        return action, (LEFT, RIGHT, MIDDLE)[data[1] - 1], 0
    elif data in (b"N\x00", b"N\x01"):
        return action, -1 if data[1] == 0 else 1, 0
    elif action == b"Z" and len(data) == 2:
        steps, = struct.unpack("!b", data[1:])
        if steps and abs(steps) <= 8:
            return action, steps, 0
    elif data in (b"D", b"R", b"E"):
        return action, 0, 0
    raise ValueError("Invalid touchpad event")


def request(object_id, opcode, *arguments):
    size = 8 + 4 * len(arguments)
    return struct.pack("=" + "I" * (len(arguments) + 2), object_id, size << 16 | opcode,
                       *(argument & 0xffffffff for argument in arguments))


def now_ms():
    return int(time.monotonic() * 1000) & 0xffffffff


class VirtualPointer:
    def __init__(self):
        display = Path(os.environ.get("WAYLAND_DISPLAY") or "wayland-0")
        if not display.is_absolute():
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime:
                raise OSError("No Wayland session")
            display = Path(runtime) / display
        self.stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.dragging = False
        self.axes = set()
        self.buffer = bytearray()
        self.manager = None
        try:
            self.stream.settimeout(0.4)
            self.stream.connect(str(display))
            self.stream.sendall(request(DISPLAY, 1, REGISTRY) + request(DISPLAY, 0, 3))
            self._sync(3)
            if self.manager is None:
                raise OSError("Virtual pointer protocol unavailable")
            name = MANAGER_INTERFACE.ljust((len(MANAGER_INTERFACE) + 3) & ~3, b"\0")
            bind = struct.pack("=II", self.manager, len(MANAGER_INTERFACE)) + name + struct.pack("=II", 1, MANAGER)
            self.stream.sendall(struct.pack("=II", REGISTRY, (8 + len(bind)) << 16) + bind
                                + request(MANAGER, 0, 0, POINTER) + request(DISPLAY, 0, 6))
            self._sync(6)
            self.stream.settimeout(0.15)
        except BaseException:
            self.stream.close()
            raise

    def _events(self):
        while len(self.buffer) >= 8:
            sender, header = struct.unpack_from("=II", self.buffer)
            size, opcode = header >> 16, header & 0xffff
            if size < 8 or size % 4:
                raise OSError("Invalid Wayland event")
            if len(self.buffer) < size:
                return
            payload = bytes(self.buffer[8:size])
            del self.buffer[:size]
            if sender == DISPLAY and opcode == 0:
                raise OSError("The compositor declined the virtual pointer")
            if sender == REGISTRY and opcode == 0 and len(payload) >= 12:
                name, length = struct.unpack_from("=II", payload)
                if payload[8:8 + length] == MANAGER_INTERFACE:
                    self.manager = name
            yield sender, opcode

    def _sync(self, callback):
        deadline = time.monotonic() + 0.4
        while True:
            for sender, opcode in self._events():
                if sender == callback and opcode == 0:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Waiting for Wayland")
            self.stream.settimeout(remaining)
            data = self.stream.recv(16384)
            if not data:
                raise OSError("The compositor closed the virtual pointer connection")
            self.buffer.extend(data)

    def frame(self, *events):
        if events:
            self.stream.sendall(b"".join(events) + request(POINTER, 4))

    def button(self, code, pressed):
        self.frame(request(POINTER, 2, now_ms(), code, int(pressed)))

    def scroll_frame(self, events):
        source = request(POINTER, 5, 1)  # wl_pointer.axis_source.finger
        # Hyprland assigns the source to the most recently supplied axis.
        self.frame(*(event + source for event in events))

    def stop_scroll(self):
        if self.axes:
            timestamp = now_ms()
            self.scroll_frame(request(POINTER, 6, timestamp, axis) for axis in sorted(self.axes))
            self.axes.clear()

    def handle(self, action, first, second):
        if action == b"M":
            self.frame(request(POINTER, 0, now_ms(), round(first * 256), round(second * 256)))
        elif action == b"S":
            axes = [(axis, round(value * 256)) for axis, value in ((0, second), (1, first)) if value]
            if axes:
                self.axes.update(axis for axis, _ in axes)
                timestamp = now_ms()
                self.scroll_frame(request(POINTER, 3, timestamp, axis, delta) for axis, delta in axes)
        elif action == b"C":
            self.button(first, True)
            self.button(first, False)
        elif action == b"D" and not self.dragging:
            self.button(LEFT, True)
            self.dragging = True
        elif action == b"R" and self.dragging:
            self.button(LEFT, False)
            self.dragging = False
        elif action == b"E":
            self.stop_scroll()

    def close(self):
        try:
            if self.dragging:
                self.button(LEFT, False)
            self.stop_scroll()
            self.stream.sendall(request(POINTER, 8) + request(MANAGER, 1))
        except OSError:
            pass
        finally:
            self.stream.close()


class PointerSession:
    def __init__(self):
        self.device = None
        self.zoom = DesktopMagnifier()
        self.retry_at = 0

    def update(self, enabled):
        if not enabled:
            self.close()
        elif self.device is None and time.monotonic() >= self.retry_at:
            try:
                self.device = VirtualPointer()
            except OSError as error:
                if not self.retry_at:
                    log.warning("Unable to start the phone touchpad: %s", error)
                self.retry_at = time.monotonic() + RETRY_DELAY
            else:
                self.retry_at = 0
        return self.device is not None

    def handle(self, event, enabled):
        if not enabled:
            self.close()
        elif event[0] == b"Z":
            return self.zoom.change(event[1])
        elif self.device is not None:
            try:
                self.device.handle(*event)
            except OSError as error:
                log.warning("Phone touchpad lost its compositor connection: %s", error)
                self.close()
                self.retry_at = time.monotonic() + RETRY_DELAY

    def close(self):
        if self.device is not None:
            self.device.close()
            self.device = None
        self.retry_at = 0
        self.zoom.close()
