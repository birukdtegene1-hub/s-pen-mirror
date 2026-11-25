import os
import threading
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORMTHEME"] = "offscreen"

from PySide6.QtWidgets import QApplication

from desktop.annotation import CANVAS_LIMIT, HISTORY_LIMIT, DesktopAnnotations, DesktopContext
from desktop.cast import CastRegion
from desktop.layer import AnnotationLayer
from desktop.receiver import LiveView


class AnnotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["annotation-tests", "-platform", "offscreen"])

    def test_overlay_maps_for_ink_and_hides_on_window_switch_and_clear(self):
        view = LiveView()
        bounds = (0, 0, 100, 100)
        region = CastRegion(20, 20, 60, 60, "DP-1", bounds, 1)
        view.show("cast")
        view.set_desktop_context("editor", bounds, region.monitor)
        view.set_region(region)
        revision, _ = view.capture_target()
        view.publish_capture(revision, b"picture")

        with patch("desktop.annotation.DesktopContext"), patch("desktop.annotation.open_layer", return_value=None):
            overlay = DesktopAnnotations(view)
        self.addCleanup(overlay.close)
        overlay.set_enabled(True)
        overlay.refresh()
        self.assertFalse(overlay.isVisible())
        self.assertEqual(overlay.canvases, {})

        stroke = {"action": "start", "x": .5, "y": .5, "pressure": 1,
                  "tool": 1, "color": 0xffff0000, "size": 10, "page_width": 100, "page_height": 100}
        view.add_annotation(stroke, revision)
        view.add_annotation({"action": "end"}, revision)
        overlay.refresh()
        self.assertTrue(overlay.isVisible())
        image = overlay.canvases["editor"].image
        self.assertGreater(image.pixelColor(53, 49).alpha(), 0)
        self.assertEqual(image.pixelColor(57, 49).alpha(), 0)

        view.set_desktop_context("browser", bounds, region.monitor)
        overlay.refresh()
        self.assertFalse(overlay.isVisible())
        view.set_desktop_context("editor", bounds, region.monitor)
        overlay.refresh()
        self.assertTrue(overlay.isVisible())

        revision, _ = view.capture_target()
        view.publish_capture(revision, b"picture")
        view.add_annotation(dict(stroke, tool=3), revision)
        view.add_annotation({"action": "end"}, revision)
        overlay.refresh()
        self.assertEqual(image.pixelColor(53, 49).alpha(), 0)
        view.add_annotation({"action": "clear"}, revision)
        overlay.refresh()
        self.assertFalse(overlay.isVisible())

    def test_native_layer_reapplies_the_empty_input_shape(self):
        layer = AnnotationLayer.__new__(AnnotationLayer)
        layer.empty_input = object()
        window = Mock()
        first, remapped = Mock(), Mock()
        window.get_window.side_effect = [first, remapped]
        layer._ignore_input(window)
        layer._ignore_input(window)
        for surface in (first, remapped):
            surface.input_shape_combine_region.assert_called_once_with(layer.empty_input, 0, 0)
            surface.set_pass_through.assert_called_once_with(True)

    def test_highlighter_overlap_preserves_opacity_and_undo(self):
        overlay = self.make_overlay()
        bounds = (0, 0, 80, 100)
        stroke = dict(action="start", x=.2, y=.5, pressure=1, tool=2,
                      color=0xffffdd00, size=4, page_width=80, page_height=100)
        overlay._apply("editor", bounds, stroke)
        overlay._apply("editor", bounds, dict(action="move", x=.8, y=.5, pressure=1))
        overlay._apply("editor", bounds, dict(action="move", x=.2, y=.5, pressure=1))
        overlay._apply("editor", bounds, {"action": "end"})
        image = overlay.canvases["editor"].image
        self.assertAlmostEqual(image.pixelColor(40, 49).alpha(), 92, delta=1)
        overlay._apply("editor", bounds, {"action": "undo"})
        self.assertEqual(image.pixelColor(40, 49).alpha(), 0)
        overlay._apply("editor", bounds, {"action": "redo"})
        self.assertAlmostEqual(image.pixelColor(40, 49).alpha(), 92, delta=1)
        overlay._apply("editor", bounds, dict(stroke, x=.5, tool=3))
        overlay._apply("editor", bounds, {"action": "cancel"})
        self.assertAlmostEqual(image.pixelColor(40, 49).alpha(), 92, delta=1)

    def test_long_running_sessions_bound_history_and_context_count(self):
        overlay = self.make_overlay()
        bounds = (0, 0, 20, 20)
        stroke = dict(action="start", x=.5, y=.5, pressure=1, tool=1,
                      color=0xff112233, size=1, page_width=20, page_height=20)
        for _ in range(HISTORY_LIMIT + 3):
            overlay._apply("editor", bounds, stroke)
            overlay._apply("editor", bounds, {"action": "end"})
        self.assertEqual(len(overlay.canvases["editor"].undo), HISTORY_LIMIT)
        for i in range(CANVAS_LIMIT + 1):
            key = str(i)
            overlay._apply(key, bounds, stroke)
            overlay._apply(key, bounds, {"action": "end"})
        overlay._prune_canvases("editor")
        self.assertEqual(len(overlay.canvases), CANVAS_LIMIT)
        self.assertIn("editor", overlay.canvases)
        self.assertNotIn("0", overlay.canvases)

    def make_overlay(self):
        with patch("desktop.annotation.DesktopContext"), patch("desktop.annotation.open_layer", return_value=None):
            overlay = DesktopAnnotations(LiveView())
        self.addCleanup(overlay.close)
        return overlay

    def test_context_refreshes_on_fragmented_focus_events(self):
        watcher = DesktopContext.__new__(DesktopContext)
        watcher.closed = threading.Event()
        stream = Mock()
        stream.recv.side_effect = [b"openlayer>>s-pen-mirror-ink\nactivewin", b"dowv2>>abc\n", b""]
        with patch.object(watcher, "_read_context") as read_context:
            watcher._follow_events(stream)
        self.assertEqual(read_context.call_count, 2)
