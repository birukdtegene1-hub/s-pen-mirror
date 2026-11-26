"""A page-aware PDF target for the paired phone's existing Write on PC view."""

import time

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from desktop.pdf_document import PREVIEW_SIDE, PdfDocument


class PdfEditor(QWidget):
    def __init__(self, view, surface, refresh_window, drain_events):
        super().__init__()
        self.view = view
        self.surface = surface
        surface.setMouseTracking(True)
        surface.installEventFilter(self)
        self.refresh_window = refresh_window
        self.drain_events = drain_events
        self.file = None
        self.serial = 0
        self.index = 0
        self.gesture = None
        self.image = QImage()
        self.last_frame = None
        self.cursor = None
        self.wheel_ticks = 0.0
        self.last_wheel = 0.0
        self.redraw = QTimer(self, interval=70, singleShot=True)
        self.redraw.timeout.connect(self.render)
        self.autosave = QTimer(self, interval=1200, singleShot=True)
        self.autosave.timeout.connect(self.save_pending)
        row = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self.turn(-1))
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self.turn(1))
        self.page_label = QLabel()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.save_button = QPushButton("Save PDF")
        self.save_button.clicked.connect(self.save_now)
        copy = QPushButton("Save a copy")
        copy.clicked.connect(self.save_copy)
        close = QPushButton("Close PDF")
        close.clicked.connect(self.close_file)
        for button in (self.previous, self.page_label, self.next):
            row.addWidget(button)
        row.addStretch()
        for button in (self.save_button, copy, close):
            row.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.surface, 1)
        layout.addWidget(self.status)
        self.shortcuts = [QShortcut(QKeySequence(key), self, activated=lambda offset=direction: self.turn(offset))
                          for key, direction in (("PgUp", -1), ("PgDown", 1), ("Left", -1), ("Right", 1))]

    def open_picker(self):
        name, _ = QFileDialog.getOpenFileName(self, "Open PDF to annotate", "", "PDF documents (*.pdf)")
        if not name or not self.confirm_close():
            return
        try:
            self.open_path(name)
        except Exception as error:
            QMessageBox.warning(self, "Open PDF", f"Could not open this PDF. {error}")

    def open_path(self, path):
        document = PdfDocument(path)
        image = document.render(0)
        self.file = document
        self.serial += 1
        self.index = 0
        self.show_page(image)

    def show_page(self, image=None):
        index = self.index
        w, h = self.file.document.get_page(index).get_size()
        self.view.set_pdf_page(((self.serial, index), (0, 0, w, h), ""))
        self.page_label.setText(f"{index + 1} / {self.file.page_count}")
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(index + 1 < self.file.page_count)
        self.image = image if image is not None else QImage()
        self.last_frame = None
        self.update_status()
        self.render()
        self.refresh_window()

    def turn(self, direction):
        if not direction or self.file is None or not 0 <= self.index + direction < self.file.page_count:
            return
        self.drain_events()
        self.finish()
        self.index += direction
        self.show_page()

    def eventFilter(self, watched, event):
        if watched is self.surface and self.file is not None:
            if event.type() == QEvent.Type.Wheel:
                pixels, angles = event.pixelDelta(), event.angleDelta()
                delta = (pixels.y() or pixels.x()) / 65 if not pixels.isNull() else (angles.y() or angles.x()) / 120
                now = time.monotonic()
                if now - self.last_wheel > .35 or self.wheel_ticks * delta < 0:
                    self.wheel_ticks = 0
                self.last_wheel = now
                self.wheel_ticks += delta
                pages = max(-3, min(3, int(self.wheel_ticks)))
                if pages:
                    self.wheel_ticks -= int(self.wheel_ticks)
                    destination = max(0, min(self.file.page_count - 1, self.index - pages))
                    self.turn(destination - self.index)
                return True
            if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Leave):
                cursor = None
                if event.type() == QEvent.Type.MouseMove:
                    target = self.surface.image_rect()
                    position = event.position()
                    if not target.isEmpty() and target.contains(position.toPoint()):
                        cursor = ((position.x() - target.x()) / target.width(),
                                  (position.y() - target.y()) / target.height())
                if cursor != self.cursor:
                    self.cursor = cursor
                    if self.view.pdf_target() is not None and not self.redraw.isActive():
                        self.redraw.start()
        return super().eventFilter(watched, event)

    def apply_events(self, events):
        redraw = False
        for key, _, _, event, _ in events:
            if self.file is None or key[0] != self.serial:
                continue
            index = key[1]
            action = event["action"]
            if action == "start":
                self.finish()
                self.autosave.stop()
                page = self.file.document.get_page(index)
                width, height = page.get_size()
                self.gesture = dict(event, index=index, dimensions=(width, height), points=[], edits={}, sweeps=[])
                redraw |= self.add_point(event)
            elif action == "move" and self.gesture is not None and self.gesture["index"] == index:
                redraw |= self.add_point(event)
            elif action in ("end", "cancel"):
                if self.gesture is not None and self.gesture["index"] == index:
                    self.finish(cancel=action == "cancel")
                    redraw = True
            elif action in ("clear", "undo", "redo"):
                self.finish()
                if action == "clear":
                    self.file.clear(index)
                else:
                    self.file.step(index, action == "redo")
                self.edited(index)
                redraw = True
        redraw |= self.flush_eraser()
        if redraw and not self.redraw.isActive():
            self.redraw.start()

    def add_point(self, event):
        gesture = self.gesture
        w, h = gesture["dimensions"]
        point = (event["x"] * w, (1 - event["y"]) * h, event["pressure"])
        points = gesture["points"]
        if points and point == points[-1]:
            return False
        previous = points[-1] if points else point
        points.append(point)
        if gesture["tool"] < 3:
            return True
        gesture["sweeps"].append((previous[:2], point[:2]))
        gesture["points"] = [point]
        return False

    def flush_eraser(self):
        gesture = self.gesture
        if gesture is None or not gesture["sweeps"]:
            return False
        index = gesture["index"]
        w, h = gesture["dimensions"]
        radius = gesture["size"] * 3.5 * max(w, h) / PREVIEW_SIDE / 2
        changes = self.file.erase(index, gesture["sweeps"], radius, gesture["tool"] == 4)
        gesture["sweeps"].clear()
        for annot, before, after in changes:
            saved = gesture["edits"].get(annot, (annot, before, before))
            gesture["edits"][annot] = (annot, saved[1], after)
        if changes and index == self.index:
            self.image = QImage()
        return bool(changes)

    def finish(self, cancel=False):
        if not cancel:
            self.flush_eraser()
        gesture = self.gesture
        self.gesture = None
        if gesture is None:
            return
        index = gesture["index"]
        if gesture["tool"] >= 3:
            changes = tuple(gesture["edits"].values())
            if cancel:
                self.file.apply(self.file.document.get_page(index), changes, reverse=True)
                if index == self.index:
                    self.image = QImage()
            else:
                self.file.remember(index, changes)
            if changes and not cancel:
                self.edited(index)
        elif not cancel and gesture["points"]:
            color, width, opacity = self.style(gesture)
            points = [point[:2] for point in gesture["points"]]
            self.file.add(index, points, (color.red(), color.green(), color.blue()), width, opacity)
            self.edited(index)
        if self.file.dirty and self.file.can_overwrite:
            self.autosave.start()

    @staticmethod
    def style(gesture):
        width = gesture["size"] * max(gesture["dimensions"]) / PREVIEW_SIDE
        tool = gesture["tool"]
        if tool == 0:
            pressure = sum(point[2] for point in gesture["points"]) / len(gesture["points"])
            width *= 0.23 + 1.35 * pressure
        return QColor.fromRgba(gesture["color"]), max(0.2, width * (3 if tool == 2 else 1)), 92 / 255 if tool == 2 else 1

    def edited(self, index):
        if index == self.index:
            self.image = QImage()
        if self.file.dirty and self.file.can_overwrite:
            self.autosave.start()
        self.update_status()

    def render(self):
        if self.file is None:
            return
        if self.image.isNull():
            self.image = self.file.render(self.index)
        shown = self.image
        gesture = self.gesture
        if gesture is not None and gesture["index"] == self.index and gesture["tool"] < 3 and gesture["points"]:
            shown = QImage(self.image)
            width, height = gesture["dimensions"]
            ratio = shown.width() / width
            pts = [QPointF(x * ratio, (height - y) * shown.height() / height) for x, y, _ in gesture["points"]]
            color, pen_width, opacity = self.style(gesture)
            color.setAlphaF(opacity)
            with QPainter(shown) as painter:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                pen = QPen(color, pen_width * ratio, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                if len(pts) == 1:
                    painter.drawPoint(pts[0])
                else:
                    path = QPainterPath(pts[0])
                    for point in pts[1:]:
                        path.lineTo(point)
                    painter.drawPath(path)
        self.surface.image = shown
        self.surface.update()
        target = self.view.pdf_target()
        if target is not None:
            revision, _ = target
            current = (revision, shown.cacheKey(), self.cursor)
            if current != self.last_frame:
                frame = self.cursor_frame(shown) if self.cursor is not None else shown
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                if frame.save(buffer, "JPEG", 89):
                    self.view.publish_capture(revision, bytes(buffer.data()))
                    self.last_frame = current

    def cursor_frame(self, shown):
        x, y = self.cursor
        frame = QImage(shown)
        shape = QPainterPath(QPointF(0, 0))
        for corner in ((0, 25), (7, 19), (13, 29), (17, 26), (12, 17), (22, 17)):
            shape.lineTo(*corner)
        shape.closeSubpath()
        with QPainter(frame) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(x * frame.width(), y * frame.height())
            painter.setPen(QPen(QColor("#172a20"), 2))
            painter.setBrush(QColor("#f8fbf9"))
            painter.drawPath(shape)
        return frame

    def update_status(self, error=None):
        if self.file is None:
            return
        if error:
            message = error
        elif not self.file.can_overwrite:
            message = "Read-only or signed PDF. Choose Save a copy to keep your ink."
        elif self.file.dirty:
            message = f"Saving ink to {self.file.path.name}…"
        else:
            message = self.file.path.name
        self.status.setText(message)
        self.save_button.setEnabled(self.file.can_overwrite and self.file.dirty)

    def save_pending(self):
        self.autosave.stop()
        if self.file is None or self.gesture is not None or not self.file.dirty or not self.file.can_overwrite:
            return
        try:
            self.file.save()
        except Exception as error:
            self.update_status(str(error))
        else:
            self.update_status()

    def save_now(self):
        self.drain_events()
        self.finish()
        self.save_pending()
        self.render()

    def save_copy(self):
        if self.file is None:
            return
        self.drain_events()
        self.finish()
        suggested = self.file.path.with_name(self.file.path.stem + " - Annotated.pdf")
        name, _ = QFileDialog.getSaveFileName(self, "Save annotated PDF", str(suggested), "PDF documents (*.pdf)")
        if not name:
            return
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        try:
            self.file.save(name)
        except Exception as error:
            self.update_status(str(error))
        else:
            self.update_status()

    def confirm_close(self):
        self.drain_events()
        self.finish()
        self.save_pending()
        if self.file is None or not self.file.dirty:
            return True
        response = QMessageBox.question(self, "Unsaved PDF ink", "Close this PDF and discard its unsaved ink?",
                                        QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                        QMessageBox.StandardButton.Cancel)
        return response == QMessageBox.StandardButton.Discard

    def close_file(self):
        if self.confirm_close():
            self.redraw.stop()
            self.file = None
            self.cursor = None
            self.view.set_pdf_page(None)
            self.refresh_window()
