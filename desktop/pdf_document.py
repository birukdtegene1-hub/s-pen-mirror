"""Editable PDF ink, using the same Poppler renderer as the PDF file."""

import ctypes
import os
import stat
import tempfile
from collections import defaultdict, deque
from ctypes.util import find_library
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QImage

from desktop.eraser import erase_paths


AUTHOR = "S-Pen Mirror"
PREVIEW_SIDE = 1920


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


@lru_cache(maxsize=1)
def backend():
    import cairo
    import gi

    gi.require_foreign("cairo")
    gi.require_version("Poppler", "0.18")
    from gi.repository import GLib, Poppler

    if not hasattr(Poppler, "AnnotInk"):
        raise ImportError("Poppler GLib 25.06 or later is required for PDF ink.")
    poppler = ctypes.CDLL(find_library("poppler-glib") or "libpoppler-glib.so.8")
    glib = ctypes.CDLL(find_library("glib-2.0") or "libglib-2.0.so.0")
    return cairo, GLib, Poppler, InkPaths(poppler, glib)


class InkPaths:
    def __init__(self, poppler, glib):
        # The current GI metadata describes the constructor's point array as a single point.
        pointer, size = ctypes.c_void_p, ctypes.c_size_t
        self.allocate = bind(glib, "g_malloc", [size], pointer)
        self.make_path = bind(poppler, "poppler_path_new_from_array", [ctypes.POINTER(Point), size], pointer)
        self.free_path = bind(poppler, "poppler_path_free", [pointer], None)
        self.set_ink = bind(poppler, "poppler_annot_ink_set_ink_list", [pointer, ctypes.POINTER(pointer), size], None)
        self.get_width = bind(poppler, "poppler_annot_get_border_width",
                              [pointer, ctypes.POINTER(ctypes.c_double)], ctypes.c_int)
        self.address = bind(ctypes.pythonapi, "PyCapsule_GetPointer", [ctypes.py_object, ctypes.c_char_p], pointer)

    def set(self, annot, paths):
        native_paths = []
        try:
            for path in paths:
                if len(path) == 1 or (len(path) == 2 and path[0] == path[1]):
                    x, y = path[0]
                    path = ((x, y), (x + 0.01, y))
                array = Point * len(path)
                points = array.from_address(self.allocate(ctypes.sizeof(array)))
                for index, (x, y) in enumerate(path):
                    points[index] = Point(x, y)
                # Poppler owns the GLib-allocated array once it has been handed to the path.
                native_paths.append(self.make_path(points, len(path)))
            native_array = (ctypes.c_void_p * len(native_paths))(*native_paths)
            self.set_ink(self.address(annot.__gpointer__, None), native_array, len(native_paths))
        finally:
            for path in native_paths:
                self.free_path(path)

    def width(self, annot):
        width = ctypes.c_double()
        found = self.get_width(self.address(annot.__gpointer__, None), ctypes.byref(width))
        return max(0, width.value) if found else 1

    def get(self, annot):
        return tuple(tuple((point.x, point.y) for point in path.get_points())
                     for path in annot.get_ink_list())


@dataclass(eq=False)
class Annotation:
    color: tuple
    width: float
    opacity: float
    flags: int
    label: str | None
    contents: str | None = None
    draw_below: bool = False
    native: object = None


class PdfDocument:
    def __init__(self, path):
        self.cairo, glib, self.poppler, self.paths = backend()
        self.path = Path(path).resolve(strict=True)
        with self.path.open("rb") as source:
            self.original = self.fingerprint(os.fstat(source.fileno()))
            self.document = self.poppler.Document.new_from_bytes(glib.Bytes.new(source.read()), None)
        if self.page_count == 0:
            raise ValueError("This PDF has no pages.")
        if not self.document.get_permissions() & self.poppler.Permissions.OK_TO_ADD_NOTES:
            raise ValueError("This PDF restricts adding annotations.")
        self.can_overwrite = os.access(self.path, os.W_OK) and not bool(self.document.get_signature_fields())
        self.dirty = False
        self.annotations = {}
        self.history = defaultdict(lambda: deque(maxlen=64))
        self.redo_history = defaultdict(lambda: deque(maxlen=64))

    @property
    def page_count(self):
        return self.document.get_n_pages()

    @staticmethod
    def fingerprint(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def render(self, index):
        page = self.document.get_page(index)
        w, h = page.get_size()
        scale = PREVIEW_SIDE / max(w, h)
        width, height = max(1, round(w * scale)), max(1, round(h * scale))
        surface = self.cairo.ImageSurface(self.cairo.FORMAT_RGB24, width, height)
        painter = self.cairo.Context(surface)
        painter.set_source_rgb(1, 1, 1)
        painter.paint()
        painter.scale(width / w, height / h)
        page.render(painter)
        surface.flush()
        return QImage(surface.get_data(), width, height, surface.get_stride(), QImage.Format.Format_RGB32).copy()

    def add(self, index, points, color, width, opacity):
        page = self.document.get_page(index)
        record = Annotation(tuple(int(channel) * 257 for channel in color), width, opacity,
                            self.poppler.AnnotFlag.PRINT, AUTHOR)
        changes = ((record, (), (tuple(points),)),)
        self.apply(page, changes)
        self.remember(index, changes)

    def create(self, page, record):
        rectangle = self.poppler.Rectangle()
        rectangle.x2, rectangle.y2 = page.get_size()
        annot = self.poppler.AnnotInk.new(self.document, rectangle)
        rgb = self.poppler.Color()
        rgb.red, rgb.green, rgb.blue = record.color
        annot.set_color(rgb)
        annot.set_border_width(record.width)
        annot.set_opacity(record.opacity)
        annot.set_flags(record.flags)
        if record.label is not None:
            annot.set_label(record.label)
        if record.contents is not None:
            annot.set_contents(record.contents)
        if record.draw_below and hasattr(annot, "set_draw_below"):
            annot.set_draw_below(True)
        page.add_annot(annot)
        self.annotations[annot] = record
        record.native = annot
        return annot

    def record_for(self, annot):
        record = self.annotations.get(annot)
        if record is None:
            color = annot.get_color()
            rgb = (color.red, color.green, color.blue) if color is not None else (0, 0, 0)
            record = Annotation(rgb, self.paths.width(annot), annot.get_opacity(), annot.get_flags(),
                                annot.get_label(), annot.get_contents(),
                                bool(hasattr(annot, "get_draw_below") and annot.get_draw_below()), annot)
            self.annotations[annot] = record
        return record

    def editable(self, page):
        flag = self.poppler.AnnotFlag
        blocked = flag.READ_ONLY | flag.LOCKED | flag.LOCKED_CONTENTS | flag.HIDDEN | flag.NO_VIEW
        return [entry.annot for entry in page.get_annot_mapping()
                if isinstance(entry.annot, self.poppler.AnnotInk) and not entry.annot.get_flags() & blocked]

    def erase(self, index, sweeps, radius, whole):
        page = self.document.get_page(index)
        changes = []
        for annot in self.editable(page):
            old = self.paths.get(annot)
            new = erase_paths(old, sweeps, radius + self.paths.width(annot) / 2, whole)
            if new != old:
                changes.append((self.record_for(annot), old, new))
        self.apply(page, changes)
        return changes

    def clear(self, index):
        page = self.document.get_page(index)
        changes = tuple((self.record_for(annot), self.paths.get(annot), ()) for annot in self.editable(page)
                        if annot.get_label() == AUTHOR)
        self.apply(page, changes)
        self.remember(index, changes)

    def apply(self, page, changes, reverse=False):
        for record, old, new in changes:
            if reverse:
                old, new = new, old
            if new:
                annot = record.native if record.native is not None else self.create(page, record)
                self.paths.set(annot, new)
            elif old and record.native is not None:
                annot = record.native
                page.remove_annot(annot)
                self.annotations.pop(annot, None)
                record.native = None

    def remember(self, index, changes):
        if changes:
            self.history[index].append(tuple(changes))
            self.redo_history[index].clear()
            self.dirty = True

    def step(self, index, forward):
        undo, redo = self.history[index], self.redo_history[index]
        source, target = (redo, undo) if forward else (undo, redo)
        if source:
            changes = source.pop()
            self.apply(self.document.get_page(index), changes, reverse=not forward)
            target.append(changes)
            self.dirty = True

    def save(self, destination=None):
        target = Path(destination).resolve() if destination is not None else self.path
        same_file = target == self.path
        if same_file and not self.can_overwrite:
            raise ValueError("Save a copy of this signed or read-only PDF.")
        if same_file and self.fingerprint(target.stat()) != self.original:
            raise ValueError("This file changed on disk. Save an annotated copy to keep both versions.")
        with tempfile.NamedTemporaryFile(prefix=".spen-", suffix=".pdf", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
        try:
            if not self.document.save(temporary.as_uri()):
                raise OSError("Poppler could not write the PDF.")
            if same_file:
                latest = target.stat()
                if self.fingerprint(latest) != self.original:
                    raise ValueError("This file changed while saving. Save an annotated copy.")
                temporary.chmod(stat.S_IMODE(latest.st_mode))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self.original = self.fingerprint(target.stat())
        self.path = target
        self.can_overwrite = True
        self.dirty = False
