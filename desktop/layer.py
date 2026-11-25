"""A click-through annotation surface anchored to a Wayland output."""

import time

from PySide6.QtCore import QAbstractEventDispatcher


def open_layer(get_layers):
    try:
        import gi
        import cairo

        gi.require_foreign("cairo")
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import Gdk, GLib, Gtk, GtkLayerShell
    except (ImportError, ValueError):
        return None
    if not Gtk.init_check()[0] or not GtkLayerShell.is_supported():
        return None
    return AnnotationLayer(get_layers, Gdk, GLib, Gtk, GtkLayerShell, cairo)


class AnnotationLayer:
    def __init__(self, get_layers, gdk, glib, gtk, shell, cairo):
        self.display = gdk.Display.get_default()
        dispatcher = QAbstractEventDispatcher.instance()
        uses_glib = dispatcher is not None and "Glib" in dispatcher.metaObject().className()
        self.context = None if uses_glib else glib.MainContext.default()
        self.shell = shell
        self.cairo = cairo
        self.empty_input = cairo.Region()
        self.get_layers = get_layers
        self.window = gtk.Window()
        self.window.set_decorated(False)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_visual(self.window.get_screen().get_rgba_visual())
        self.window.set_app_paintable(True)
        shell.init_for_window(self.window)
        shell.set_namespace(self.window, "s-pen-mirror-ink")
        shell.set_layer(self.window, shell.Layer.OVERLAY)
        shell.set_exclusive_zone(self.window, -1)
        shell.set_keyboard_mode(self.window, shell.KeyboardMode.NONE)
        for edge in (shell.Edge.TOP, shell.Edge.BOTTOM, shell.Edge.LEFT, shell.Edge.RIGHT):
            shell.set_anchor(self.window, edge, True)
        self.window.connect("realize", self._ignore_input)
        self.window.connect_after("map", self._ignore_input)
        self.window.connect_after("size-allocate", self._ignore_input)
        self.window.connect("draw", self._draw)

    def place(self, bounds, connector):
        x, y, width, height = bounds
        matches = []
        screen = self.window.get_screen()
        for index in range(self.display.get_n_monitors()):
            monitor = self.display.get_monitor(index)
            rect = monitor.get_geometry()
            if abs(rect.x - x) <= 2 and abs(rect.y - y) <= 2:
                named = screen.get_monitor_plug_name(index) == connector
                distance = abs(rect.x - x) + abs(rect.y - y) + abs(rect.width - width) + abs(rect.height - height)
                matches.append((not named, distance, index, monitor))
        if not matches:
            self.window.hide()
            return
        monitor = min(matches)[-1]
        if self.shell.get_monitor(self.window) != monitor:
            self.shell.set_monitor(self.window, monitor)
        self.window.show()

    def update(self):
        self.window.queue_draw()

    def dispatch(self):
        if self.context is None:
            return
        deadline = time.monotonic() + 0.002
        while time.monotonic() < deadline and self.context.iteration(False):
            pass

    def _ignore_input(self, window, *_):
        surface = window.get_window()
        if surface is not None:
            # Restore the empty native region on map as GTK can recreate the wl_surface.
            surface.input_shape_combine_region(self.empty_input, 0, 0)
            surface.set_pass_through(True)

    def _draw(self, window, painter):
        painter.set_operator(self.cairo.OPERATOR_SOURCE)
        painter.set_source_rgba(0, 0, 0, 0)
        painter.paint()
        painter.set_operator(self.cairo.OPERATOR_OVER)
        for image, opacity in self.get_layers():
            if image.isNull():
                continue
            surface = self.cairo.ImageSurface.create_for_data(
                image.bits(), self.cairo.FORMAT_ARGB32,
                image.width(), image.height(), image.bytesPerLine(),
            )
            painter.save()
            painter.scale(window.get_allocated_width() / image.width(),
                          window.get_allocated_height() / image.height())
            painter.set_source_surface(surface)
            painter.paint_with_alpha(opacity)
            painter.restore()
        return True
