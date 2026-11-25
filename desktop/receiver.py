"""Local TLS receiver for the S-Pen Mirror Android app."""

import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader, QPainter


MAX_FRAME = 8 * 1024 * 1024
MAX_SIDE = 2048
CAST_PROTOCOL = "spen-region-1"
CANCEL_PROTOCOL = "spen-region-2"
PAIRING_TIMEOUT = 3
CLIPBOARD_TIMEOUT = 90
CAST_SELECT, CAST_WAIT, CAST_ERROR, CAST_READY, CAST_PAUSED = range(5)


def lan_address():
    """Prefer an up Wi-Fi interface, then a physical LAN, then other IPv4."""
    try:
        interfaces = json.loads(subprocess.check_output(
            ["ip", "-j", "-4", "address", "show", "up"], stderr=subprocess.DEVNULL
        ))
    except (OSError, ValueError, subprocess.CalledProcessError):
        interfaces = []
    choices = []
    for interface in interfaces:
        name = interface["ifname"]
        sysfs = Path("/sys/class/net") / name
        rank = 0 if (sysfs / "wireless").exists() else 1 if (sysfs / "device").exists() else 2
        for info in interface.get("addr_info", []):
            if info["scope"] == "global" and info["family"] == "inet":
                choices.append((rank, name, info["local"]))
    if choices:
        return min(choices)[2]
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ipaddress.ip_address(address).is_loopback:
                return address
    except OSError:
        pass
    raise ValueError("Connect to your LAN or pass --host with the PC’s LAN IPv4 address.")


def tls_identity():
    with tempfile.TemporaryDirectory(prefix="spen-") as directory:
        cert, key = (Path(directory) / name for name in ("cert.pem", "key.pem"))
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
            "ec_paramgen_curve:prime256v1", "-nodes", "-sha256", "-days", "7",
            "-subj", "/CN=S-Pen Mirror", "-keyout", str(key), "-out", str(cert),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols([CANCEL_PROTOCOL, CAST_PROTOCOL])
        context.load_cert_chain(cert, key)
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        return context, fingerprint


class LiveView:
    """A one-frame mailbox. Slow GUI repaints never accumulate frames."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.mode = "pair"
        self.image = QImage()
        self.desktop_context = ("desktop", (0, 0, 1920, 1080), "")
        self.annotation_events = []
        self.annotation_origin = None
        self.region = None
        self.cast_revision = 0
        self.cast_status = CAST_SELECT
        self.cast_image = None
        self.capture_errors = 0

    def show(self, mode, image=None):
        with self.lock:
            if self.mode in ("write", "cast"):
                self._finish_annotation()
            self.mode = mode
            self.image = QImage(image) if image is not None else QImage()
            if self.region is not None or mode == "cast":
                self.region = None
                self._invalidate_cast()
            self.version += 1

    def after(self, version):
        with self.lock:
            if self.version == version:
                return None
            return self.version, self.mode, QImage(self.image)

    def set_desktop_context(self, key, bounds, monitor):
        with self.lock:
            context = (key, bounds, monitor)
            if context != self.desktop_context:
                self.desktop_context = context
                if self.region is not None:
                    self._invalidate_cast()

    def current_desktop_context(self):
        with self.lock:
            return self.desktop_context

    def current_region(self):
        with self.lock:
            return self.region

    def set_region(self, region):
        with self.lock:
            if self.mode != "cast":
                return False
            self.region = region
            self._invalidate_cast()
            return True

    def _invalidate_cast(self):
        self.cast_revision += 1
        self.cast_image = None
        self.capture_errors = 0
        if self.region is None:
            self.cast_status = CAST_SELECT
        elif self._region_on_active_display():
            self.cast_status = CAST_WAIT
        else:
            self.cast_status = CAST_PAUSED
        self.version += 1

    def _region_on_active_display(self):
        _, bounds, monitor = self.desktop_context
        return (monitor, bounds) == (self.region.monitor, self.region.bounds)

    def capture_target(self):
        with self.lock:
            if self.region is not None and self._region_on_active_display():
                return self.cast_revision, self.region
            return None

    def publish_capture(self, revision, data):
        with self.lock:
            if self.region is None or revision != self.cast_revision:
                return False
            previous = self.cast_status
            changed = False
            if data is None:
                self.capture_errors += 1
                if self.capture_errors < 3:
                    return False
                if self.cast_status == CAST_READY:
                    self._invalidate_cast()
                self.cast_status = CAST_ERROR
            else:
                self.capture_errors = 0
                self.cast_status = CAST_READY
                if data != self.cast_image:
                    self.cast_image = data
                    changed = True
            if self.cast_status != previous:
                self.version += 1
            return changed

    def capture_update(self):
        with self.lock:
            return self.cast_status, self.cast_revision, self.cast_image

    def add_annotation(self, event, revision=None):
        with self.lock:
            action = event["action"]
            if self.mode not in ("cast", "write"):
                return
            if action in ("end", "cancel"):
                if self.annotation_origin is not None and self.annotation_origin[-1] == revision:
                    self._finish_annotation(action)
                return
            if self.mode == "cast":
                if revision != self.cast_revision or self.cast_status != CAST_READY:
                    return
                event = self.region.map_annotation(event)
            if action != "move":
                self._finish_annotation()
            if action == "move":
                if self.annotation_origin is None or len(self.annotation_events) >= 1023:
                    return
                context = self.annotation_origin
            else:
                if len(self.annotation_events) >= 1023:
                    return
                context = (*self.desktop_context, revision)
                if action == "start":
                    self.annotation_origin = context
            self._queue_annotation(context, event)

    def _queue_annotation(self, context, event):
        key, bounds, monitor, revision = context
        self.annotation_events.append((key, bounds, monitor, event, revision))

    def _finish_annotation(self, action="end"):
        if self.annotation_origin is not None:
            self._queue_annotation(self.annotation_origin, {"action": action})
            self.annotation_origin = None

    def take_annotation_update(self):
        with self.lock:
            events = self.annotation_events
            self.annotation_events = []
            return self.desktop_context, self.cast_revision, events


def remaining_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Receive deadline exceeded")
    return remaining


def read_exact(stream, size, deadline=None):
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            stream.settimeout(remaining_time(deadline))
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def decode_image(data, image_format):
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, image_format)
    size = reader.size()
    if not (0 < size.width() <= MAX_SIDE and 0 < size.height() <= MAX_SIDE):
        raise ValueError("Unsupported image dimensions")
    image = reader.read()
    if image.isNull():
        raise ValueError("Incomplete image")
    return image


def decode_annotation(data, cancel_supported=False):
    action = data[:1]
    if action == b"S" and len(data) == 30:
        _, x, y, pressure, tool, color, size, page_width, page_height = struct.unpack(
            "!cfffBifII", data
        )
        if tool > 3 or not (1 <= page_width <= 10000 and 1 <= page_height <= 10000):
            raise ValueError("Invalid annotation settings")
        if not (1 <= size <= 36):
            raise ValueError("Invalid annotation width")
        values = (x, y, pressure)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Invalid annotation point")
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= pressure <= 1):
            raise ValueError("Annotation point outside screen")
        return {"action": "start", "x": x, "y": y, "pressure": pressure,
                "tool": tool, "color": color & 0xffffffff, "size": size,
                "page_width": page_width, "page_height": page_height}
    if action == b"M" and len(data) == 13:
        _, x, y, pressure = struct.unpack("!cfff", data)
        if not all(math.isfinite(value) for value in (x, y, pressure)):
            raise ValueError("Invalid annotation point")
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= pressure <= 1):
            raise ValueError("Annotation point outside screen")
        return {"action": "move", "x": x, "y": y, "pressure": pressure}
    if action in (b"E", b"C", b"U", b"R") and len(data) == 1:
        return {"action": {b"E": "end", b"C": "clear", b"U": "undo", b"R": "redo"}[action]}
    if cancel_supported and data == b"X":
        return {"action": "cancel"}
    raise ValueError("Invalid annotation event")


def receive_frames(stream, view, cast_supported=False, cancel_supported=False):
    mode = "ink"
    page = QImage()
    last_capture = None
    last_revision = None
    view.show(mode)
    while True:
        kind, size = struct.unpack("!cI", read_exact(stream, 5))
        if size > MAX_FRAME:
            raise ValueError("Frame exceeds size limit")
        if kind in (b"H", b"B", b"M", b"W"):
            if size:
                raise ValueError("Unexpected control data")
            if kind != b"H":
                mode = {b"B": "ink", b"M": "screen", b"W": "write"}[kind]
                page = QImage()
                view.show(mode)
        elif cast_supported and kind == b"V" and size == 0:
            mode = "cast"
            page = QImage()
            last_capture = None
            last_revision = None
            view.show(mode)
        elif mode == "cast" and kind == b"F" and size == 0:
            status, revision, picture = view.capture_update()
            data = picture if picture is not last_capture or revision != last_revision else None
            stream.sendall(struct.pack("!cBII", b"+", status, revision, len(data or b"")))
            if data:
                stream.sendall(data)
            last_capture = picture
            last_revision = revision
            continue
        elif mode == "ink" and kind == b"I":
            page = decode_image(read_exact(stream, size), b"png").convertToFormat(
                QImage.Format.Format_RGB32
            )
            view.show(mode, page)
        elif mode == "ink" and kind == b"P" and size > 4 and not page.isNull():
            left, top = struct.unpack("!HH", read_exact(stream, 4))
            patch = decode_image(read_exact(stream, size - 4), b"png")
            if left + patch.width() > page.width() or top + patch.height() > page.height():
                raise ValueError("Patch outside page")
            with QPainter(page) as painter:
                painter.drawImage(left, top, patch)
            view.show(mode, page)
        elif mode == "screen" and kind == b"S":
            view.show(mode, decode_image(read_exact(stream, size), b"jpeg"))
        elif mode in ("write", "cast") and kind == b"D":
            prefix = 4 if mode == "cast" else 0
            if not prefix < size <= prefix + 30:
                raise ValueError("Invalid annotation event size")
            data = read_exact(stream, size)
            revision = struct.unpack("!I", data[:prefix])[0] if prefix else None
            view.add_annotation(decode_annotation(data[prefix:], cancel_supported), revision)
        else:
            raise ValueError("Unexpected frame")
        stream.sendall(b"+")


def authenticate(stream, tokens, deadline=None):
    greeting = read_exact(stream, 39, deadline)
    matched = None
    for token in tokens:
        if hmac.compare_digest(greeting, f"SPEN1 {token}\n".encode("ascii")):
            matched = token
    if deadline is not None:
        stream.settimeout(remaining_time(deadline))
    if matched is None:
        stream.sendall(b"!")
    return matched


def receive_session(stream, token, active, view, cast_supported=False):
    if authenticate(stream, (token,)):
        receive_paired(stream, active, view, cast_supported)


def receive_paired(stream, active, view, cast_supported, cancel_supported=False, authorize=None):
    if not active.acquire(blocking=False):
        stream.sendall(b"*")
        return
    try:
        stream.settimeout(15)
        token = authorize() if authorize is not None else ""
        if token is None:
            stream.sendall(b"!")
            return
        stream.sendall(b"+" + (token.encode("ascii") if cancel_supported else b""))
        receive_frames(stream, view, cast_supported, cancel_supported)
    finally:
        view.show("pair")
        active.release()


class Receiver:
    def __init__(self, address, port, view):
        self.context, fingerprint = tls_identity()
        self.token = secrets.token_hex(16)
        self.fingerprint = fingerprint
        self.clipboard_token = None
        self.clipboard_expiry = 0
        self.reconnect_token = None
        self.view = view
        self.active = threading.Lock()
        self.admission = threading.Lock()
        self.pending = {}
        self.retry_after = OrderedDict()
        self.connections = set()
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((address, port))
            self.listener.listen(24)
            self.listener.settimeout(0.5)
        except OSError:
            self.listener.close()
            raise
        self.address, self.port = self.listener.getsockname()
        self.link = self.pairing_link(self.token)

    def pairing_link(self, token):
        return f"spenmirror://{self.address}:{self.port}/v1/{token}/{self.fingerprint}"

    def issue_clipboard_link(self):
        with self.admission:
            self.clipboard_token = secrets.token_hex(16)
            self.clipboard_expiry = time.monotonic() + CLIPBOARD_TIMEOUT
            return self.pairing_link(self.clipboard_token)

    def active_tokens(self):
        with self.admission:
            tokens = [self.token]
            if self.reconnect_token is not None:
                tokens.append(self.reconnect_token)
            if self.clipboard_token is not None and time.monotonic() < self.clipboard_expiry:
                tokens.append(self.clipboard_token)
            return tokens

    def authorize(self, token, reconnect_supported):
        with self.admission:
            if token in (self.token, self.reconnect_token):
                return token
            if token != self.clipboard_token or time.monotonic() >= self.clipboard_expiry:
                return None
            if reconnect_supported:
                self.clipboard_token = None
                self.reconnect_token = secrets.token_hex(16)
                return self.reconnect_token
            return token

    def start(self):
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.closed.is_set():
            try:
                raw, (host, _) = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            deadline = time.monotonic() + PAIRING_TIMEOUT
            if not self.admit(host, raw):
                raw.close()
                continue
            try:
                threading.Thread(target=self.serve, args=(raw, host, deadline), daemon=True).start()
            except RuntimeError:
                self.release_pending(host)
                with self.admission:
                    self.connections.discard(raw)
                raw.close()

    def admit(self, host, raw):
        with self.admission:
            if (self.closed.is_set() or len(self.connections) >= 13 or self.pending.get(host, 0) >= 2
                    or time.monotonic() < self.retry_after.get(host, 0)):
                return False
            self.pending[host] = self.pending.get(host, 0) + 1
            self.connections.add(raw)
            return True

    def release_pending(self, host, failed=False):
        with self.admission:
            self.pending[host] -= 1
            if not self.pending[host]:
                del self.pending[host]
            if failed:
                self.retry_after[host] = time.monotonic() + 0.25
                self.retry_after.move_to_end(host)
                if len(self.retry_after) > 256:
                    self.retry_after.popitem(last=False)

    def serve(self, raw, host, deadline):
        stream = raw
        pending = True
        try:
            with raw:
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.admission:
                    if self.closed.is_set():
                        return
                    stream = self.context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                    self.connections.remove(raw)
                    self.connections.add(stream)
                with stream:
                    stream.settimeout(remaining_time(deadline))
                    stream.do_handshake()
                    token = authenticate(stream, self.active_tokens(), deadline)
                    if token is None:
                        return
                    self.release_pending(host)
                    pending = False
                    protocol = stream.selected_alpn_protocol()
                    receive_paired(stream, self.active, self.view,
                                   protocol in (CAST_PROTOCOL, CANCEL_PROTOCOL), protocol == CANCEL_PROTOCOL,
                                   lambda: self.authorize(token, protocol == CANCEL_PROTOCOL))
        except (OSError, EOFError, ValueError):
            pass
        finally:
            if pending:
                self.release_pending(host, failed=True)
            with self.admission:
                self.connections.discard(stream)

    def close(self):
        self.closed.set()
        self.listener.close()
        with self.admission:
            for stream in self.connections:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
