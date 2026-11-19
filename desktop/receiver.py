"""Local TLS receiver for the S-Pen Mirror Android app."""

import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader, QPainter


MAX_FRAME = 8 * 1024 * 1024
MAX_SIDE = 2048


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
        context.minimum_version = ssl.TLSVersion.TLSv1_2
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

    def show(self, mode, image=None):
        with self.lock:
            self.mode = mode
            self.image = QImage(image) if image is not None else QImage()
            self.version += 1

    def after(self, version):
        with self.lock:
            if self.version == version:
                return None
            return self.version, self.mode, QImage(self.image)


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
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


def receive_frames(stream, view):
    mode = "ink"
    page = QImage()
    view.show(mode)
    while True:
        kind, size = struct.unpack("!cI", read_exact(stream, 5))
        if size > MAX_FRAME:
            raise ValueError("Frame exceeds size limit")
        if kind in (b"H", b"B", b"M"):
            if size:
                raise ValueError("Unexpected control data")
            if kind != b"H":
                mode = "ink" if kind == b"B" else "screen"
                page = QImage()
                view.show(mode)
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
        else:
            raise ValueError("Unexpected frame")
        stream.sendall(b"+")


def receive_session(stream, token, active, view):
    greeting = f"SPEN1 {token}\n".encode("ascii")
    if not hmac.compare_digest(read_exact(stream, len(greeting)), greeting):
        stream.sendall(b"!")
        return
    if not active.acquire(blocking=False):
        stream.sendall(b"*")
        return
    try:
        stream.settimeout(15)
        stream.sendall(b"+")
        receive_frames(stream, view)
    finally:
        view.show("pair")
        active.release()


class Receiver:
    def __init__(self, address, port, view):
        self.context, fingerprint = tls_identity()
        self.token = secrets.token_hex(16)
        self.view = view
        self.active = threading.Lock()
        self.capacity = threading.BoundedSemaphore(6)
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((address, port))
            self.listener.listen(6)
            self.listener.settimeout(0.5)
        except OSError:
            self.listener.close()
            raise
        self.address, self.port = self.listener.getsockname()
        self.link = f"spenmirror://{address}:{self.port}/v1/{self.token}/{fingerprint}"

    def start(self):
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.closed.is_set():
            try:
                raw, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if self.capacity.acquire(blocking=False):
                threading.Thread(target=self.serve, args=(raw,), daemon=True).start()
            else:
                raw.close()

    def serve(self, raw):
        try:
            with raw:
                raw.settimeout(6)
                if raw.family == socket.AF_INET:
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.context.wrap_socket(raw, server_side=True) as stream:
                    receive_session(stream, self.token, self.active, self.view)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.capacity.release()

    def close(self):
        self.closed.set()
        self.listener.close()
