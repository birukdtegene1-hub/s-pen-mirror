import hashlib
import io
import os
import ssl
import struct
import threading
import unittest
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_QPA_PLATFORMTHEME"] = "offscreen"

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from desktop.cast import CastRegion
from desktop.receiver import (
    CANCEL_PROTOCOL, CAST_ERROR, CAST_PAUSED, CAST_PROTOCOL, CAST_READY, CAST_SELECT, CAST_WAIT, MAX_FRAME,
    LiveView, Receiver, decode_annotation, read_exact, receive_frames, receive_paired, receive_session, tls_identity,
)


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

    def test_cast_polls_send_only_new_frames(self):
        shot = picture("blue", (40, 24), "JPEG")
        region = CastRegion(10, 20, 40, 24, "DP-1", (0, 0, 100, 100), 1)

        class CastStream(Stream):
            def sendall(stream, data):
                super().sendall(data)
                # Select the region after the first poll, as a PC user would.
                if data.startswith(b"+" + bytes([CAST_SELECT])):
                    self.view.set_desktop_context("editor", region.bounds, region.monitor)
                    self.view.set_region(region)
                    self.view.publish_capture(self.view.capture_target()[0], shot)

        requests = self.hello + frame(b"V") + frame(b"F") * 3
        stream = CastStream(requests)
        with self.assertRaises(EOFError):
            receive_session(stream, self.token, self.active, self.view, cast_supported=True)
        waiting = struct.pack("!cBII", b"+", CAST_SELECT, 1, 0)
        new_picture = struct.pack("!cBII", b"+", CAST_READY, 2, len(shot)) + shot
        same_picture = struct.pack("!cBII", b"+", CAST_READY, 2, 0)
        self.assertEqual(stream.reply, b"++" + waiting + new_picture + same_picture)
        self.assertIsNone(self.view.capture_target())
        self.assertEqual(self.view.capture_update()[0], CAST_SELECT)
        with self.assertRaises(ValueError):
            receive_session(Stream(requests), self.token, self.active, self.view)

    def test_touchpad_has_its_own_cast_capability_and_packets(self):
        self.view.set_pdf_page(((1, 0), (0, 0, 600, 400), ""))
        pointer = Mock()
        pointer.update.return_value = True
        stream = Stream(frame(b"V") + frame(b"F") + frame(b"T", b"C\x01") + frame(b"B"))
        with self.assertRaises(EOFError):
            receive_frames(stream, self.view, cast_supported=True, pointer=pointer)
        reply, status, _revision, jpeg_size, flags = struct.unpack("!cBIIB", stream.reply[1:-2])
        self.assertEqual((reply, status, jpeg_size, flags), (b"+", CAST_WAIT, 0, 1))
        self.assertEqual(stream.reply[-2:], b"++")
        pointer.update.assert_called_once_with(True)
        pointer.handle.assert_called_once_with((b"C", 0x110, 0), True)
        self.assertEqual(pointer.close.call_count, 2)

        with self.assertRaisesRegex(ValueError, "Unexpected frame"):
            receive_frames(Stream(frame(b"V") + frame(b"T", b"C\x01")), self.view, cast_supported=True)
        with self.assertRaisesRegex(ValueError, "Unexpected frame"):
            receive_frames(Stream(frame(b"T", b"C\x01")), self.view, cast_supported=True, pointer=pointer)

    def test_monitor_swipe_requires_negotiation_and_leaves_a_shared_pdf_in_place(self):
        pdf = ((1, 0), (0, 0, 600, 400), "")
        self.view.set_pdf_page(pdf)
        swipe = frame(b"V") + frame(b"T", b"N\x00")
        pointer = Mock()
        pointer.update.return_value = True
        with patch("desktop.receiver.focus_adjacent_monitor") as focus:
            with self.assertRaisesRegex(ValueError, "requires the negotiated protocol"):
                receive_frames(Stream(swipe), self.view, cast_supported=True, pointer=pointer)
            focus.assert_not_called()
            stream = Stream(swipe)
            with self.assertRaises(EOFError):
                receive_frames(stream, self.view, cast_supported=True, pointer=pointer, monitor_supported=True)
            self.assertEqual(stream.reply, b"++")
            focus.assert_called_once_with(None, -1)
            self.assertEqual(self.view.pdf_target()[1], pdf)
            pointer.handle.assert_not_called()

    def test_monitor_swipe_casts_the_destination_and_restores_the_crop_on_return(self):
        left = dict(name="DP-1", width=1920, height=1080, x=-1920, y=0, scale=1)
        right = dict(name="eDP-1", width=1200, height=1800, x=0, y=120, scale=1.25, transform=1)
        crop = CastRegion.from_selection("-1800,100 600x480", [left])
        full = CastRegion.from_monitor(right)
        self.view.show("cast")
        self.view.set_desktop_context("left window", crop.bounds, crop.monitor)
        self.view.set_region(crop)
        previous, _ = self.view.capture_target()
        with patch("desktop.receiver.focus_adjacent_monitor", side_effect=[right, left]) as focus:
            self.view.switch_monitor(1)
            self.assertEqual(self.view.current_region(), full)
            self.assertFalse(self.view.publish_capture(previous, b"late frame"))
            self.view.set_desktop_context("right window", full.bounds, full.monitor)
            self.assertEqual(self.view.capture_target()[1], full)
            self.view.switch_monitor(-1)
            self.assertEqual(focus.call_args_list, [call("DP-1", 1), call("eDP-1", -1)])
        self.assertEqual(self.view.current_region(), crop)

    def test_magnifier_packets_are_negotiated_and_cast_ink_maps_back_to_the_desktop(self):
        self.view.set_pdf_page(((1, 0), (0, 0, 600, 400), ""))
        pointer = Mock()
        pointer.handle.return_value = 2
        pinch = frame(b"V") + frame(b"T", b"Z\x03")
        with self.assertRaisesRegex(ValueError, "requires the negotiated protocol"):
            receive_frames(Stream(pinch), self.view, cast_supported=True, pointer=pointer, monitor_supported=True)
        stream = Stream(pinch)
        with self.assertRaises(EOFError):
            receive_frames(stream, self.view, cast_supported=True, pointer=pointer,
                           monitor_supported=True, magnifier_supported=True)
        self.assertEqual(stream.reply, b"++")
        pointer.handle.assert_called_once_with((b"Z", 3, 0), True)

        self.view.set_pdf_page(None)
        self.view.set_cast_zoom(1)
        region = CastRegion(200, 100, 400, 250, "DP-1", (-100, -50, 1920, 1080), 1)
        self.view.set_desktop_context("editor", region.bounds, region.monitor)
        self.view.set_region(region)
        old_revision, _ = self.view.capture_target()
        self.view.set_cast_zoom(2)
        revision, _ = self.view.capture_target()
        self.assertNotEqual(old_revision, revision)
        self.assertFalse(self.view.publish_capture(old_revision, b"old screenshot"))
        self.assertEqual(self.view.zoom_at(revision), 2)
        self.view.publish_capture(revision, b"zoomed screenshot", (2, 700, 450))
        self.view.add_annotation(dict(action="start", x=1, y=0, tool=0), revision)
        point = self.view.take_annotation_update()[2][0][3]
        self.assertAlmostEqual(point["x"] * 1919, 699.5)
        self.assertAlmostEqual(point["y"] * 1079, 300)
        self.assertEqual(point["clip"], (500 / 1920, 300 / 1080, 200 / 1920, 125 / 1080))

    def test_cast_ink_uses_selected_region_and_rejects_stale_input(self):
        monitor = {"name": "DP-1", "x": -1440, "y": 30, "width": 1200,
                   "height": 1800, "scale": 1.25, "transform": 1}
        region = CastRegion.from_selection("-1340,230 300x400", [monitor])
        self.assertEqual(region.bounds, (-1440, 30, 1440, 960))
        with self.assertRaises(ValueError):
            CastRegion.from_selection("-100,50 300x400", [monitor])
        rounded = CastRegion.from_selection("-1441,30 1442x960", [monitor])
        self.assertEqual((rounded.x, rounded.width), (-1440, 1440))
        self.view.show("cast")
        self.view.set_desktop_context("editor", region.bounds, region.monitor)
        self.view.set_region(region)
        revision, _ = self.view.capture_target()
        event = {"action": "start", "x": 0, "y": 1, "tool": 0}
        self.view.add_annotation(event, revision)
        self.assertEqual(self.view.take_annotation_update()[2], [])
        self.view.publish_capture(revision, b"first picture")
        self.view.add_annotation(event, revision)
        key, bounds, display, point, target = self.view.take_annotation_update()[2][0]
        self.assertEqual((key, bounds, display, target), ("editor", region.bounds, region.monitor, revision))
        self.assertAlmostEqual(point["x"] * (bounds[2] - 1), 100)
        self.assertAlmostEqual(point["y"] * (bounds[3] - 1), 599)
        self.assertEqual((point["page_width"], point["page_height"]), bounds[2:])

        self.view.set_desktop_context("other app", region.bounds, region.monitor)
        self.view.publish_capture(revision, b"late picture")
        self.assertIsNone(self.view.capture_update()[2])
        new_revision, _ = self.view.capture_target()
        self.view.publish_capture(new_revision, b"new picture")
        self.view.add_annotation(event, revision)
        self.assertEqual(self.view.take_annotation_update()[2], [])
        self.view.set_desktop_context("other display", (0, 0, 1920, 1080), "eDP-1")
        self.assertEqual(self.view.capture_update()[0], CAST_PAUSED)
        self.assertIsNone(self.view.capture_target())

    def test_transient_capture_failure_keeps_the_frame_and_revision(self):
        self.view.show("cast")
        region = CastRegion(10, 20, 40, 24, "DP-1", (0, 0, 100, 100), 1)
        self.view.set_desktop_context("editor", region.bounds, region.monitor)
        self.view.set_region(region)
        revision, _ = self.view.capture_target()
        self.view.publish_capture(revision, b"picture")
        for _ in range(2):
            self.view.publish_capture(revision, None)
            self.assertEqual(self.view.capture_update(), (CAST_READY, revision, b"picture"))
        self.view.publish_capture(revision, None)
        self.assertEqual(self.view.capture_update(), (CAST_ERROR, revision + 1, None))

    def test_legacy_stroke_keeps_its_original_context(self):
        self.view.show("write")
        bounds = (0, 0, 100, 100)
        self.view.set_desktop_context("editor", bounds, "DP-1")
        self.view.add_annotation({"action": "start"})
        self.view.set_desktop_context("browser", bounds, "DP-1")
        self.view.add_annotation({"action": "move"})
        self.view.add_annotation({"action": "end"})
        self.view.add_annotation({"action": "undo"})
        context, _, events = self.view.take_annotation_update()
        self.assertEqual(context[0], "browser")
        self.assertEqual([(key, event["action"]) for key, _, _, event, _ in events],
                         [("editor", "start"), ("editor", "move"), ("editor", "end"), ("browser", "undo")])

    def test_deadline_shared_across_fragmented_reads(self):
        stream = Stream(b"a partial frame")
        with patch("desktop.receiver.time.monotonic", side_effect=[1, 4]):
            with self.assertRaises(TimeoutError):
                read_exact(stream, 14, deadline=3)

    def test_cancel_requires_negotiated_protocol(self):
        with self.assertRaises(ValueError):
            decode_annotation(b"X")
        self.assertEqual(decode_annotation(b"X", cancel_supported=True), {"action": "cancel"})

    def test_copied_pairing_link_rotates_to_a_private_reconnect_token(self):
        listener = Mock()
        listener.getsockname.return_value = ("192.168.1.20", 47831)
        with patch("desktop.receiver.tls_identity", return_value=(None, "a" * 64)), \
                patch("desktop.receiver.socket.socket", return_value=listener):
            receiver = Receiver("192.168.1.20", 47831, self.view)
        with patch("desktop.receiver.time.monotonic", return_value=1):
            manual = receiver.issue_clipboard_link().split("/")[-2]
            self.assertNotEqual(manual, receiver.token)
            self.assertIn(manual, receiver.active_tokens())
            stream = Stream(frame(b"H"))
            with self.assertRaises(EOFError):
                receive_paired(stream, self.active, self.view, True, True,
                               lambda: receiver.authorize(manual, True))
            reconnect = receiver.reconnect_token
            self.assertNotIn(manual, receiver.active_tokens())
            self.assertEqual(reconnect, receiver.authorize(reconnect, True))
            self.assertEqual(stream.reply, b"+" + reconnect.encode("ascii") + b"+")
            expired = receiver.issue_clipboard_link().split("/")[-2]
        with patch("desktop.receiver.time.monotonic", return_value=92):
            self.assertNotIn(expired, receiver.active_tokens())
            self.assertIsNone(receiver.authorize(expired, True))

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
        client_ctx.set_alpn_protocols([CAST_PROTOCOL, CANCEL_PROTOCOL])
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
        self.assertEqual(client.selected_alpn_protocol(), CANCEL_PROTOCOL)
        self.assertEqual(client.version(), "TLSv1.3")
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
