import hashlib
import io
import os
import ssl
import struct
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_QPA_PLATFORMTHEME"] = "offscreen"

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from desktop.receiver import MAX_FRAME, LiveView, receive_session, tls_identity


def picture(color, size, fmt):
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(buffer.data())


def frame(kind, payload=b""):
    return struct.pack("!cI", kind, len(payload)) + payload


class Stream:
    """Fragment reads so framing gets exercised independently of TCP packet boundaries."""
    def __init__(self, data):
        self.source = io.BytesIO(data)
        self.reply = bytearray()

    def recv(self, size):
        return self.source.read(min(7, size))

    def sendall(self, data):
        self.reply.extend(data)

    def settimeout(self, seconds):
        pass


class Recorder(LiveView):
    def __init__(self):
        super().__init__()
        self.history = []

    def show(self, mode, image=None):
        super().show(mode, image)
        self.history.append(self.after(-1))


class ProtocolTests(unittest.TestCase):
    token = "f" * 32
    hello = f"SPEN1 {token}\n".encode()

    def setUp(self):
        self.active = threading.Lock()
        self.view = Recorder()

    def test_live_patches_switching_and_disconnect(self):
        page = picture("#ffffff", (32, 24), "PNG")
        patch = picture("#135e51", (6, 3), "PNG")
        screen = picture("#2288cc", (12, 20), "JPEG")
        data = self.hello + frame(b"B") + frame(b"I", page)
        data += frame(b"P", struct.pack("!HH", 8, 5) + patch) + frame(b"H")
        data += frame(b"M") + frame(b"S", screen) + frame(b"B") + frame(b"I", page)
        stream = Stream(data)
        with self.assertRaises(EOFError):
            receive_session(stream, self.token, self.active, self.view)
        views = self.view.history
        self.assertEqual(stream.reply, b"+" * 9)
        self.assertEqual(views[2][2].pixelColor(8, 5).name(), "#ffffff")
        self.assertEqual(views[3][2].pixelColor(8, 5).name(), "#135e51")
        self.assertEqual(views[3][2].pixelColor(7, 5).name(), "#ffffff")
        self.assertEqual((views[4][1], views[4][2].isNull()), ("screen", True))
        self.assertEqual((views[5][2].width(), views[5][2].height()), (12, 20))
        self.assertEqual(views[-2][1], "ink")
        self.assertEqual(views[-2][2].pixelColor(8, 5).name(), "#ffffff")
        self.assertEqual((views[-1][1], views[-1][2].isNull()), ("pair", True))
        self.assertFalse(self.active.locked())

    def test_exclusive_pairing(self):
        wrong = Stream(self.hello.replace(b"ff", b"00", 1))
        receive_session(wrong, self.token, self.active, self.view)
        self.assertEqual(wrong.reply, b"!")
        with self.active:
            busy = Stream(self.hello)
            receive_session(busy, self.token, self.active, self.view)
            self.assertEqual(busy.reply, b"*")
        self.assertEqual(self.view.history, [])

    def test_invalid_or_incomplete_frame_releases_session(self):
        for data, error in [
            (struct.pack("!cI", b"I", MAX_FRAME + 1), ValueError),
            (frame(b"P", b"abcd0"), ValueError),
            (frame(b"H", b"x"), ValueError),
            (frame(b"I", picture("white", (5, 5), "PNG"))
             + frame(b"P", struct.pack("!HH", 4, 4) + picture("red", (3, 3), "PNG")), ValueError),
            (struct.pack("!cI", b"I", 100) + b"partial", EOFError),
        ]:
            with self.subTest(data=data[:5]), self.assertRaises(error):
                receive_session(Stream(self.hello + data), self.token, self.active, self.view)
            self.assertFalse(self.active.locked())
            self.assertTrue(self.view.image.isNull())

    def test_certificate_pin_and_encrypted_session(self):
        server_ctx, pin = tls_identity()
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
        client = client_ctx.wrap_bio(ci, co, server_side=False)
        server = server_ctx.wrap_bio(si, so, server_side=True)
        for _ in range(8):
            for side, out, other in ((client, co, si), (server, so, ci)):
                try:
                    side.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                other.write(out.read())
            if server.version() and client.version():
                break
        self.assertEqual(hashlib.sha256(client.getpeercert(binary_form=True)).hexdigest(), pin)
        client.write(self.hello + frame(b"H"))
        ciphertext = co.read()
        self.assertNotIn(self.hello, ciphertext)
        si.write(ciphertext)

        class EncryptedStream:
            def settimeout(self, seconds): pass
            def sendall(self, payload): server.write(payload)
            def recv(self, size):
                try: return server.read(size)
                except ssl.SSLWantReadError: return b""

        with self.assertRaises(EOFError):
            receive_session(EncryptedStream(), self.token, self.active, self.view)
        ci.write(so.read())
        self.assertEqual(client.read() + client.read(), b"++")


if __name__ == "__main__":
    unittest.main()
