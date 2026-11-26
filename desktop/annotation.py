"""Transparent Hyprland overlay for remote S-Pen strokes."""

import json
import math
import os
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPainterPathStroker, QPen, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from desktop.cast import monitor_bounds
from desktop.layer import open_layer

HISTORY_LIMIT = 64
CANVAS_LIMIT = 16
CACHE_BYTES = 192 * 1024 * 1024
HIGHLIGHT_OPACITY = 92 / 255


class DesktopContext:
    EVENTS = {b"activewindow", b"activewindowv2", b"focusedmon", b"focusedmonv2", b"workspace",
              b"workspacev2", b"fullscreen", b"monitoradded", b"monitorremoved", b"movewindow",
              b"moveworkspace", b"activespecial", b"configreloaded"}

    def __init__(self, view):
        self.view = view
        self._monitor_lock = threading.Lock()
        self._monitors = ()
        self._updated = 0
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="hyprland-context", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()

    def monitors_snapshot(self):
        with self._monitor_lock:
            return self._monitors if time.monotonic() - self._updated < 3 else ()

    def _watch(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        event_path = Path(runtime) / "hypr" / signature / ".socket2.sock"
        while not self.closed.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                    stream.settimeout(1)
                    stream.connect(str(event_path))
                    self._follow_events(stream)
            except OSError:
                self._read_context()
            self.closed.wait(0.25)

    def _follow_events(self, stream):
        self._read_context()
        last_refresh = time.monotonic()
        pending = b""
        while not self.closed.is_set():
            changed = False
            try:
                chunk = stream.recv(8192)
                if not chunk:
                    return
                *lines, pending = (pending + chunk).split(b"\n")
                changed = any(line.partition(b">>")[0] in self.EVENTS for line in lines)
            except TimeoutError:
                pass
            if changed or time.monotonic() - last_refresh >= 1:
                self._read_context()
                last_refresh = time.monotonic()

    def _read_context(self):
        try:
            active, monitors = (json.loads(subprocess.check_output(
                ["hyprctl", "-j", command], stderr=subprocess.DEVNULL, timeout=0.4,
            )) for command in ("activewindow", "monitors"))
            with self._monitor_lock:
                self._monitors = tuple(monitors)
                self._updated = time.monotonic()
            active_monitor = active.get("monitor")
            monitor = next((item for item in monitors if item.get("focused")), None)
            if monitor is None:
                monitor = next((item for item in monitors if item["id"] == active_monitor), None)
            if monitor is None:
                self._unavailable()
                return
            if active_monitor != monitor["id"]:
                active = {}
            workspace = active.get("workspace") or monitor.get("activeWorkspace") or {}
            address = active.get("address") or "no-window"
            fullscreen = active.get("fullscreen", 0)
            fullscreen_client = active.get("fullscreenClient", 0)
            key = f"{address}|{workspace.get('id', -1)}|{monitor['name']}|{fullscreen}|{fullscreen_client}"
            self.view.set_desktop_context(key, monitor_bounds(monitor), monitor["name"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, ZeroDivisionError):
            if not self.monitors_snapshot():
                self._unavailable()

    def _unavailable(self):
        key, bounds, _ = self.view.current_desktop_context()
        self.view.set_desktop_context(key, bounds, "")


@dataclass
class InkEdit:
    bounds: QRectF
    before: QImage
    after: QImage
    had_ink: bool
    has_ink: bool
    previous_marks: tuple
    next_marks: tuple

    @property
    def bytes(self):
        return self.before.sizeInBytes() + self.after.sizeInBytes() + 8 * (len(self.previous_marks) + len(self.next_marks))


class CanvasState:
    def __init__(self, width, height):
        self.image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        self.image.fill(Qt.GlobalColor.transparent)
        self.undo = []
        self.redo = []
        self.has_ink = False
        self.marks = ()
        self.history_bytes = 0

    def remember(self, before, dirty, had_ink, previous_marks):
        if dirty.isEmpty():
            return
        self.history_bytes -= sum(edit.bytes for edit in self.redo)
        self.redo.clear()
        width, height = self.image.width(), self.image.height()
        bounds = QRectF(dirty.x() / width, dirty.y() / height, dirty.width() / width, dirty.height() / height)
        edit = InkEdit(bounds, before.copy(dirty), self.image.copy(dirty), had_ink, self.has_ink,
                      previous_marks, self.marks)
        self.undo.append(edit)
        self.history_bytes += edit.bytes
        if len(self.undo) > HISTORY_LIMIT:
            self.drop_oldest()

    def drop_oldest(self):
        history = self.undo or self.redo
        self.history_bytes -= history.pop(0).bytes

    def step(self, forward):
        source, target = (self.redo, self.undo) if forward else (self.undo, self.redo)
        if not source:
            return
        edit = source.pop()
        target.append(edit)
        width, height = self.image.width(), self.image.height()
        clip = edit.bounds
        rect = QRectF(clip.x() * width, clip.y() * height, clip.width() * width, clip.height() * height)
        with QPainter(self.image) as painter:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, edit.after if forward else edit.before)
        self.has_ink = edit.has_ink if forward else edit.had_ink
        self.marks = edit.next_marks if forward else edit.previous_marks


class DesktopAnnotations(QWidget):
    def __init__(self, view, pdf_events=None):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.view = view
        self.pdf_events = pdf_events
        self.context_watcher = DesktopContext(view)
        self.canvases = OrderedDict()
        self.current_key = None
        self.current_bounds = None
        self.current_monitor = ""
        self.active_key = None
        self.active_stroke = None
        self.enabled = False
        self.setWindowTitle("S-Pen Mirror Annotation")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.layer = open_layer(self._current_layers)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def close(self):
        self.set_enabled(False)
        if self.layer is not None:
            self.layer.window.destroy()
            self.layer = None
        self.context_watcher.close()
        self.timer.stop()
        self.hide()

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.current_bounds = None
        if not enabled:
            if self.layer is not None:
                self.layer.window.hide()
            else:
                self.hide()

    def _geometry(self, bounds, monitor_name):
        screen = next((item for item in QApplication.screens() if item.name() == monitor_name), None)
        if screen is not None:
            return screen.geometry()
        return QRect(*bounds)

    def _state(self, key, bounds):
        _, _, width, height = bounds
        state = self.canvases.get(key)
        if state is None:
            state = CanvasState(width, height)
            self.canvases[key] = state
        elif state.image.width() != width or state.image.height() != height:
            if key == self.active_key:
                self._finish_stroke()
            state.image = state.image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        self.canvases.move_to_end(key)
        return state

    def refresh(self):
        if self.layer is not None:
            self.layer.dispatch()
        (key, bounds, monitor), _, events = self.view.take_annotation_update()
        pdf = []
        for entry in events:
            event_key, event_bounds, _, event, _ = entry
            if isinstance(event_key, tuple):
                pdf.append(entry)
            else:
                self._apply(event_key, event_bounds, event)
        if pdf and self.pdf_events is not None:
            self.pdf_events(pdf)
        if events:
            self._prune_canvases(key)
        if not self.enabled:
            return
        placement_changed = (bounds, monitor) != (self.current_bounds, self.current_monitor)
        context_changed = (key, bounds, monitor) != (self.current_key, self.current_bounds, self.current_monitor)
        if context_changed:
            self.current_key = key
            self.current_bounds = bounds
            self.current_monitor = monitor
            if key in self.canvases:
                self._state(key, bounds)
        if context_changed or events:
            self._redraw(placement_changed)

    def _redraw(self, placement_changed):
        layers = self._current_layers()
        if self.layer is not None:
            if not layers or not self.current_monitor:
                self.layer.window.hide()
                return
            if placement_changed or not self.layer.window.get_visible():
                self.layer.place(self.current_bounds, self.current_monitor)
            self.layer.update()
        elif not layers:
            self.hide()
        else:
            if placement_changed or not self.isVisible():
                self.setGeometry(self._geometry(self.current_bounds, self.current_monitor))
                self.show()
            self.update()

    def _current_layers(self):
        state = self.canvases.get(self.current_key)
        if not self.enabled or state is None:
            return ()
        layers = [(state.image, 1)] if state.has_ink else []
        if self.active_key == self.current_key and self.active_stroke["mask"] is not None:
            layers.append((self.active_stroke["mask"], HIGHLIGHT_OPACITY))
        return layers

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for image, opacity in self._current_layers():
                painter.setOpacity(opacity)
                painter.drawImage(self.rect(), image)

    def _apply(self, key, bounds, event):
        action = event["action"]
        if action == "start":
            self._finish_stroke()
            existing = self.canvases.get(key)
            if event["tool"] >= 3 and (existing is None or not existing.has_ink):
                return
            state = self._state(key, bounds)
            stroke = {name: event[name] for name in
                      ("tool", "color", "size", "page_width", "page_height")}
            mask = None
            if stroke["tool"] == 2:
                mask = QImage(state.image.size(), state.image.format())
                mask.fill(Qt.GlobalColor.transparent)
            stroke.update(clip=event.get("clip"), previous=None, dirty=QRect(), before=QImage(state.image),
                          had_ink=state.has_ink, previous_marks=state.marks, mask=mask, coverage=QPainterPath())
            stroke["coverage"].setFillRule(Qt.FillRule.WindingFill)
            self.active_key = key
            self.active_stroke = stroke
            self._add_point(state, stroke, event)
        elif action == "move":
            if self.active_key != key or self.active_stroke is None:
                return
            self._add_point(self.canvases[key], self.active_stroke, event)
        elif action in ("end", "cancel"):
            if self.active_key == key:
                self._finish_stroke(cancel=action == "cancel")
        elif key in self.canvases:
            self._finish_stroke()
            state = self._state(key, bounds)
            if action == "clear" and state.has_ink:
                before = QImage(state.image)
                previous_marks = state.marks
                state.image.fill(Qt.GlobalColor.transparent)
                state.has_ink = False
                state.marks = ()
                state.remember(before, state.image.rect(), True, previous_marks)
            elif action in ("undo", "redo"):
                state.step(action == "redo")

    def _add_point(self, state, stroke, event):
        point = (event["x"], event["y"], event["pressure"])
        previous = stroke["previous"]
        if previous == point:
            return
        stroke["previous"] = point
        coverage = self._footprint(state.image, stroke, previous, point)
        if stroke["tool"] == 4:
            if stroke["clip"] is not None:
                coverage = coverage.intersected(self._clip_path(stroke["clip"]))
            stroke["dirty"] |= self._erase_strokes(state, coverage)
            return
        stroke["coverage"].addPath(coverage)
        mask = stroke["mask"]
        stroke["dirty"] |= self._paint_segment(state.image if mask is None else mask, stroke, previous, point)
        state.has_ink |= stroke["tool"] not in (2, 3)

    def _stroke_width(self, image, stroke, pressure):
        scale = math.sqrt((image.width() / stroke["page_width"])
                          * (image.height() / stroke["page_height"]))
        width = stroke["size"] * scale
        if stroke["tool"] == 0:
            width *= 0.23 + 1.35 * max(0, min(1, pressure))
        elif stroke["tool"] == 2:
            width *= 3
        elif stroke["tool"] >= 3:
            width *= 3.5
        return max(1, width)

    @staticmethod
    def _clip_path(clip):
        shape = QPainterPath()
        shape.addRect(QRectF(*clip))
        return shape

    def _footprint(self, image, stroke, previous, point):
        image_w, image_h = image.width(), image.height()
        previous = previous or point
        first = QPointF(previous[0] * (image_w - 1), previous[1] * (image_h - 1))
        last = QPointF(point[0] * (image_w - 1), point[1] * (image_h - 1))
        pressure = point[2] if first == last else (point[2] + previous[2]) / 2
        width = self._stroke_width(image, stroke, pressure)
        if first == last:
            shape = QPainterPath()
            shape.addEllipse(last, width / 2, width / 2)
        else:
            line = QPainterPath(first)
            line.lineTo(last)
            stroker = QPainterPathStroker()
            stroker.setWidth(width)
            stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
            shape = stroker.createStroke(line)
        return QTransform.fromScale(1 / image_w, 1 / image_h).map(shape)

    @staticmethod
    def _erase_strokes(state, eraser):
        kept = []
        dirty = QRectF()
        for path, color in state.marks:
            if path.intersects(eraser):
                dirty = dirty.united(path.boundingRect())
            else:
                kept.append((path, color))
        if len(kept) == len(state.marks):
            return QRect()
        state.marks = tuple(kept)
        state.has_ink = bool(kept)
        w, h = state.image.width(), state.image.height()
        pixels = QTransform.fromScale(w, h).mapRect(dirty).toAlignedRect().adjusted(-2, -2, 2, 2)
        pixels = pixels.intersected(state.image.rect())
        with QPainter(state.image) as painter:
            painter.setClipRect(pixels)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(pixels, Qt.GlobalColor.black)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(w, h)
            for path, color in kept:
                if path.boundingRect().intersects(dirty.adjusted(-2 / w, -2 / h, 2 / w, 2 / h)):
                    painter.fillPath(path, QColor.fromRgba(color))
        return pixels

    def _paint_segment(self, image, stroke, previous, point):
        pressure = point[2] if previous is None or previous[:2] == point[:2] else (point[2] + previous[2]) / 2
        width = self._stroke_width(image, stroke, pressure)
        color = QColor.fromRgba(stroke["color"])
        if stroke["tool"] == 2:
            color.setAlpha(255)
        clip = image.rect()
        with QPainter(image) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if stroke["clip"] is not None:
                x, y, clip_width, clip_height = stroke["clip"]
                selection = QRectF(x * image.width(), y * image.height(),
                                   clip_width * image.width(), clip_height * image.height())
                painter.setClipRect(selection)
                clip = clip.intersected(selection.toAlignedRect())
            if stroke["tool"] == 3:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                color = QColor(0, 0, 0, 255)
            pen = QPen(color)
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x = point[0] * max(0, image.width() - 1)
            y = point[1] * max(0, image.height() - 1)
            px = x if previous is None else previous[0] * max(0, image.width() - 1)
            py = y if previous is None else previous[1] * max(0, image.height() - 1)
            if (px, py) == (x, y):
                painter.drawPoint(QPointF(x, y))
            else:
                painter.drawLine(QLineF(px, py, x, y))
        margin = width / 2 + 2
        dirty = QRectF(min(x, px) - margin, min(y, py) - margin,
                       abs(x - px) + 2 * margin, abs(y - py) + 2 * margin)
        return clip.intersected(dirty.toAlignedRect())

    def _finish_stroke(self, cancel=False):
        if self.active_stroke is None:
            return
        state = self.canvases[self.active_key]
        stroke = self.active_stroke
        self.active_key = None
        self.active_stroke = None
        dirty, mask, before = stroke["dirty"], stroke["mask"], stroke["before"]
        if dirty.isEmpty():
            return
        if cancel:
            if mask is None:
                with QPainter(state.image) as painter:
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
                    painter.drawImage(dirty, before, dirty)
            state.has_ink = stroke["had_ink"]
            state.marks = stroke["previous_marks"]
            return
        if mask is not None:
            with QPainter(state.image) as painter:
                painter.setOpacity(HIGHLIGHT_OPACITY)
                painter.drawImage(dirty, mask, dirty)
            state.has_ink = True
        coverage = stroke["coverage"]
        if stroke["clip"] is not None:
            coverage = coverage.intersected(self._clip_path(stroke["clip"]))
        if stroke["tool"] < 3 and not coverage.isEmpty():
            color = QColor.fromRgba(stroke["color"])
            if stroke["tool"] == 2:
                color.setAlpha(92)
            state.marks += ((coverage, color.rgba()),)
        elif stroke["tool"] == 3:
            cut = []
            for path, color in state.marks:
                remaining = path.subtracted(coverage) if path.intersects(coverage) else path
                if not remaining.isEmpty():
                    cut.append((remaining, color))
            state.marks = tuple(cut)
            state.has_ink = bool(cut)
        state.remember(before, dirty, stroke["had_ink"], stroke["previous_marks"])

    def _prune_canvases(self, displayed):
        protected = {displayed, self.active_key}
        total = sum(state.image.sizeInBytes() + state.history_bytes for state in self.canvases.values())
        for key in tuple(self.canvases):
            if total <= CACHE_BYTES and len(self.canvases) <= CANVAS_LIMIT:
                break
            if key not in protected:
                state = self.canvases.pop(key)
                total -= state.image.sizeInBytes() + state.history_bytes
        for state in self.canvases.values():
            while total > CACHE_BYTES and state.history_bytes:
                previous = state.history_bytes
                state.drop_oldest()
                total -= previous - state.history_bytes
