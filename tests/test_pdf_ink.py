import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORMTHEME"] = "offscreen"

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from desktop.pdf_document import PdfDocument, backend
from desktop.receiver import CAST_READY, LiveView
from desktop.window import MirrorWindow


class PdfInkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cairo = backend()[0]
        except (ImportError, OSError, ValueError, AttributeError) as error:
            raise unittest.SkipTest(str(error)) from error
        cls.app = QApplication.instance() or QApplication(["pdf-ink-tests", "-platform", "offscreen"])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "Slides.pdf"
        surface = self.cairo.PDFSurface(str(self.path), 600, 400)
        draw = self.cairo.Context(surface)
        for page in ("First page", "Second page"):
            draw.set_font_size(24)
            draw.move_to(30, 45)
            draw.show_text(page)
            draw.show_page()
        surface.finish()

    def test_cast_page_changes_persist_native_ink_on_its_own_slide(self):
        view = LiveView()
        receiver = SimpleNamespace(link="spenmirror://192.168.1.1/test", address="192.168.1.1", port=47831)
        with patch("desktop.annotation.DesktopContext"), patch("desktop.annotation.open_layer", return_value=None):
            window = MirrorWindow(receiver, view)
        self.addCleanup(window.shutdown)
        window.pdf.open_path(self.path)
        view.show("cast")
        window.refresh()
        status, first_revision, screenshot = view.capture_update()
        self.assertEqual(status, CAST_READY)
        self.assertTrue(screenshot.startswith(b"\xff\xd8"))
        draw = dict(action="start", x=.2, y=.35, pressure=1, tool=1, color=0xff146238,
                    size=10, page_width=1440, page_height=900)
        view.add_annotation(draw, first_revision)
        view.add_annotation(dict(action="move", x=.75, y=.35, pressure=1), first_revision)
        wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, -65), QPoint(),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        self.app.sendEvent(window.pdf.surface, wheel)
        self.assertEqual(window.pdf.index, 1)
        status, second_revision, picture = view.capture_update()
        self.assertEqual(status, CAST_READY)
        self.assertNotEqual(second_revision, first_revision)
        self.assertNotEqual(screenshot, picture)
        view.add_annotation(draw, first_revision)
        window.pdf.save_now()
        exported = PdfDocument(self.path)
        first, second = (exported.document.get_page(i) for i in range(2))
        self.assertEqual(len(exported.editable(first)), 1)
        self.assertEqual(len(exported.editable(second)), 0)
        ink = exported.editable(first)[0]
        self.assertEqual(exported.paths.get(ink), (((120.0, 260.0), (450.0, 260.0)),))
        self.assertIn("Second page", second.get_text())
        pixel = exported.render(0).pixelColor(900, 448)
        self.assertGreater(pixel.green(), pixel.red())

    def test_native_ink_erasing_undo_and_reopen(self):
        doc = PdfDocument(self.path)
        doc.add(0, ((80, 200), (220, 200)), (20, 120, 80), 6, 1)
        doc.add(0, ((80, 140), (220, 140)), (20, 120, 80), 6, 1)
        page = doc.document.get_page(0)
        area = doc.erase(0, (((140, 195), (140, 205)),), 10, False)
        doc.remember(0, area)
        self.assertEqual(len(area), 1)
        self.assertEqual(area[0][2], (((80.0, 200.0), (127.0, 200.0)), ((153.0, 200.0), (220.0, 200.0))))
        stroke = doc.erase(0, (((90, 195), (90, 205)),), 10, True)
        doc.remember(0, stroke)
        self.assertEqual(len(doc.editable(page)), 1)
        doc.step(0, False)
        self.assertEqual(len(doc.editable(page)), 2)
        doc.save()
        opened = PdfDocument(self.path)
        annotations = opened.editable(opened.document.get_page(0))
        self.assertEqual(sorted(len(opened.paths.get(annot)) for annot in annotations), [1, 2])

    def test_saving_detects_external_file_changes(self):
        doc = PdfDocument(self.path)
        doc.add(0, ((80, 200), (200, 200)), (20, 120, 80), 6, 1)
        with self.path.open("ab") as stream:
            stream.write(b"\n% external update\n")
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            doc.save()
        self.assertTrue(self.path.read_bytes().endswith(b"% external update\n"))
        copy = self.path.with_name("Annotated.pdf")
        doc.save(copy)
        reopened = PdfDocument(copy)
        self.assertEqual(len(reopened.editable(reopened.document.get_page(0))), 1)
