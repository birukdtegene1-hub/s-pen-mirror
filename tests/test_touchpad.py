import json
import struct
import unittest
from unittest.mock import Mock, call, patch

from desktop.cast import adjacent_monitor, focus_adjacent_monitor
from desktop.magnifier import DesktopMagnifier, ZoomSettings, current_zoom, set_zoom
from desktop.pointer import LEFT, MANAGER_INTERFACE, POINTER, PointerSession, VirtualPointer, decode_pointer, request


class PointerTests(unittest.TestCase):
    def test_adjacent_monitor_uses_display_positions_and_leaves_outer_edge_alone(self):
        def monitor(name, index, x, y=0, **options):
            return dict(name=name, id=index, x=x, y=y, width=1920, height=1080, scale=1, **options)

        left, center, right = monitor("A", 2, -1920), monitor("B", 0, 0, focused=True), monitor("C", 1, 1920)
        monitors = [center, right, left, monitor("stacked", 3, 200, -1080),
                    monitor("mirror", 4, 1900, mirrorOf="C"), monitor("asleep", 5, 1700, dpmsStatus=False)]
        self.assertIs(adjacent_monitor(monitors, "B", -1), left)
        self.assertIs(adjacent_monitor(monitors, None, 1), right)
        self.assertIsNone(adjacent_monitor(monitors, "A", -1))
        with patch("desktop.cast.subprocess.check_output", return_value=json.dumps(monitors)), \
                patch("desktop.cast.subprocess.run", side_effect=[Mock(returncode=1), Mock(returncode=0, stdout=b"ok\n")]) as run:
            self.assertEqual(focus_adjacent_monitor("B", 1), right)
            self.assertEqual(run.call_args_list[0].args[0], ["hyprctl", "dispatch", "hl.dsp.focus({ monitor = 1 })"])
            self.assertEqual(run.call_args_list[1].args[0], ["hyprctl", "dispatch", "focusmonitor", "1"])

    def test_swipe_on_one_display_changes_workspaces(self):
        monitor = dict(name="eDP-1", id=0, focused=True, activeWorkspace={"id": 1})
        with patch("desktop.cast.subprocess.check_output", return_value=json.dumps([monitor])), \
                patch("desktop.cast.dispatch_focus", return_value=True) as dispatch:
            self.assertIsNone(focus_adjacent_monitor("eDP-1", -1))
            dispatch.assert_not_called()
            self.assertIsNone(focus_adjacent_monitor("eDP-1", 1))
            dispatch.assert_called_once_with("workspace = 2, on_current_monitor = true", "workspace", "2")

    def test_wayland_device_bootstraps_and_releases_drag_and_scroll(self):
        name = MANAGER_INTERFACE.ljust((len(MANAGER_INTERFACE) + 3) & ~3, b"\0")
        payload = struct.pack("=II", 85, len(MANAGER_INTERFACE)) + name + struct.pack("=I", 2)
        registry_event = struct.pack("=II", 2, (8 + len(payload)) << 16) + payload
        banner = registry_event + request(3, 0, 0)
        stream = Mock()
        stream.recv.side_effect = [banner[:11], banner[11:], request(6, 0, 0)]
        with patch("desktop.pointer.socket.socket", return_value=stream), \
                patch.dict("desktop.pointer.os.environ", {"WAYLAND_DISPLAY": "/tmp/test-wayland"}), \
                patch("desktop.pointer.now_ms", return_value=42):
            pointer = VirtualPointer()
            pointer.handle(b"D", 0, 0)
            pointer.handle(b"M", -3.5, 2.25)
            pointer.handle(b"S", 4, -8)
            pointer.close()
        frame = request(POINTER, 4)
        sent = [call.args[0] for call in stream.sendall.call_args_list]
        self.assertIn(request(4, 0, 0, POINTER), sent[1])
        self.assertEqual(sent[2], request(POINTER, 2, 42, LEFT, 1) + frame)
        self.assertEqual(sent[3], request(POINTER, 0, 42, -896, 576) + frame)
        finger = request(POINTER, 5, 1)
        self.assertEqual(sent[4], request(POINTER, 3, 42, 0, -2048) + finger
                         + request(POINTER, 3, 42, 1, 1024) + finger + frame)
        self.assertEqual(sent[5], request(POINTER, 2, 42, LEFT, 0) + frame)
        self.assertEqual(sent[6], request(POINTER, 6, 42, 0) + finger
                         + request(POINTER, 6, 42, 1) + finger + frame)
        stream.close.assert_called_once()

    def test_pointer_is_released_when_the_pc_stops_sharing(self):
        device = Mock()
        session = PointerSession()
        with patch("desktop.pointer.VirtualPointer", return_value=device) as factory:
            self.assertFalse(session.update(False))
            factory.assert_not_called()
            self.assertTrue(session.update(True))
            session.handle((b"D", 0, 0), enabled=True)
            session.handle((b"C", LEFT, 0), enabled=False)
            device.handle.assert_called_once_with(b"D", 0, 0)
            device.close.assert_called_once()
            self.assertFalse(session.update(False))

    def test_malformed_pointer_events_are_rejected(self):
        for event in (b"C\x00", b"C\x04", b"M", b"R\x00", b"M" + struct.pack("!ff", float("nan"), 1),
                      b"S" + struct.pack("!ff", 4097, 1), b"Z\x00", b"Z\x09", b"Z\xf7"):
            with self.subTest(event=event), self.assertRaises(ValueError):
                decode_pointer(event)
        self.assertEqual(decode_pointer(b"C\x01"), (b"C", LEFT, 0))
        self.assertEqual(decode_pointer(b"Z\xff"), (b"Z", -1, 0))

    def test_unavailable_compositor_is_retried_while_sharing(self):
        session = PointerSession()
        device = Mock()
        with patch("desktop.pointer.VirtualPointer", side_effect=[OSError("unavailable"), device]) as factory, \
                patch("desktop.pointer.time.monotonic", return_value=10) as clock:
            with self.assertLogs("desktop.pointer", "WARNING"):
                self.assertFalse(session.update(True))
            clock.return_value = 12
            self.assertFalse(session.update(True))
            factory.assert_called_once()
            clock.return_value = 15
            self.assertTrue(session.update(True))
            session.handle((b"C", LEFT, 0), enabled=True)
            device.handle.assert_called_once_with(b"C", LEFT, 0)


class MagnifierTests(unittest.TestCase):
    def test_magnifier_uses_hyprland_lua_and_restores_the_original_settings(self):
        base = ZoomSettings(1, True, True)
        zoom = ZoomSettings(1.5625, False, False)
        magnifier = DesktopMagnifier()
        with patch("desktop.magnifier.current_zoom", side_effect=[base, zoom]), \
                patch("desktop.magnifier.set_zoom") as set_level:
            self.assertEqual(magnifier.change(2), 1.5625)
            magnifier.close()
        self.assertEqual(set_level.call_args_list, [call(zoom), call(base)])

        with patch("desktop.magnifier.subprocess.run", return_value=Mock(returncode=0, stdout=b"1.5625 false true\n")):
            self.assertEqual(current_zoom(), ZoomSettings(1.5625, False, True))
        with patch("desktop.magnifier.hyprctl_ok", return_value=True) as request:
            set_zoom(zoom)
        request.assert_called_once_with("eval", "hl.config({ cursor = { zoom_factor = 1.56250, "
                                                "zoom_detached_camera = false, zoom_rigid = false } })")

    def test_magnifier_zoom_out_stops_at_one_and_respects_manual_changes(self):
        base = ZoomSettings(2, True, False)
        magnifier = DesktopMagnifier()
        with patch("desktop.magnifier.current_zoom", side_effect=[base, ZoomSettings(3, True, False)]), \
                patch("desktop.magnifier.set_zoom") as set_level:
            self.assertEqual(magnifier.change(-8), 1)
            magnifier.close()
        set_level.assert_called_once_with(ZoomSettings(1, True, False))
