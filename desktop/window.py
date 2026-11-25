import datetime
import shutil
import subprocess

from PySide6.QtCore import QMimeData, QProcess, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QStackedWidget, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from desktop.cast import CastRegion, RegionCapture
from desktop.receiver import CAST_ERROR, CAST_PAUSED, CAST_READY, CLIPBOARD_TIMEOUT


class Surface(QWidget):
    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.message = "Waiting for the phone’s picture…"
        self.setMinimumSize(160, 180)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), QColor("#222927"))
            if self.image.isNull():
                painter.setPen(QColor("#d3dcd5"))
                flags = Qt.AlignmentFlag.AlignCenter.value | Qt.TextFlag.TextWordWrap.value
                painter.drawText(self.rect().adjusted(24, 0, -24, 0), flags, self.message)
                return
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = self.rect()
            target.setSize(size)
            target.moveCenter(self.rect().center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self.image)


class MirrorWindow(QMainWindow):
    def __init__(self, receiver, view):
        super().__init__()
        from desktop.annotation import DesktopAnnotations

        self.receiver, self.view = receiver, view
        self.selector = None
        self.hidden_for_cast = False
        self.version = -1
        self.mode = ""
        self.copied_link = None
        self.setWindowTitle("S-Pen Mirror")
        self.resize(900, 700)
        self.setMinimumSize(400, 480)
        self.setStyleSheet("""
            QMainWindow, QWidget#root { background: #f3f4ef; color: #18271f; }
            QLabel { color: #18271f; background: transparent; }
            QFrame#bar { background: #fafbf7; border-bottom: 1px solid #d6dcd4; }
            QPushButton { background: #fafbf7; color: #193329; border: 1px solid #b8c5bb;
                          padding: 7px 13px; border-radius: 6px; }
            QPushButton:hover { background: #e6ede6; }
            QPushButton:focus { border: 2px solid #326b53; }
            QLabel#status { color: #41664f; }
        """)
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar = QFrame(objectName="bar")
        bar_layout = QHBoxLayout(self.bar)
        bar_layout.setContentsMargins(18, 10, 18, 10)
        brand = QLabel("S-Pen Mirror")
        brand.setFont(QFont("Inter", 13, QFont.Weight.DemiBold))
        bar_layout.addWidget(brand)
        bar_layout.addStretch()
        self.status = QLabel(objectName="status")
        bar_layout.addWidget(self.status)
        self.region_button = QPushButton("Select region")
        self.region_button.clicked.connect(self.select_region)
        bar_layout.addWidget(self.region_button)
        self.stop_button = QPushButton("Stop casting")
        self.stop_button.clicked.connect(self.stop_cast)
        bar_layout.addWidget(self.stop_button)
        self.save_button = QPushButton("Save PNG")
        self.save_button.clicked.connect(self.save)
        bar_layout.addWidget(self.save_button)
        self.full_button = QPushButton("Full screen")
        self.full_button.setToolTip("F11 · Esc to return")
        self.full_button.clicked.connect(self.toggle_fullscreen)
        bar_layout.addWidget(self.full_button)
        layout.addWidget(self.bar)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.stack.addWidget(self.pairing_page())
        self.annotation = DesktopAnnotations(view)
        self.capture = RegionCapture(view)
        self.surface = Surface()
        self.stack.addWidget(self.surface)
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
        self.tray.setToolTip("S-Pen Mirror is casting")
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction("Show S-Pen Mirror", self.show_controls)
        self.tray_menu.addAction("Stop casting", self.stop_cast)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.full_shortcut = QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        self.escape = QShortcut(QKeySequence("Escape"), self, activated=self.leave_fullscreen)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def select_region(self):
        if self.selector is not None or self.mode != "cast":
            return
        if not shutil.which("grim") or not shutil.which("slurp") or self.annotation.layer is None:
            QMessageBox.information(self, "Select region",
                                    "Run S-Pen Mirror in Hyprland with grim, slurp, "
                                    "gtk-layer-shell, python-gobject and python-cairo installed.")
            return
        self.view.set_region(None)
        selector = QProcess(self)
        selector.setStandardInputFile(QProcess.nullDevice())
        selector.finished.connect(lambda code, _: self.selection_finished(selector, code))
        selector.errorOccurred.connect(lambda _: self.selection_finished(selector, -1))
        self.selector = selector
        self.hide()
        selector.start("slurp", ["-d"])

    def selection_finished(self, selector, code):
        if self.selector is not selector:
            return
        self.selector = None
        data = bytes(selector.readAllStandardOutput()).decode(errors="replace")
        error_output = bytes(selector.readAllStandardError()).decode(errors="replace")
        selector.deleteLater()
        if self.mode != "cast":
            self.show_controls()
            return
        if code == 0:
            try:
                region = CastRegion.from_selection(data, self.annotation.context_watcher.monitors_snapshot())
            except ValueError as error:
                self.show_controls()
                QMessageBox.information(self, "Select region", str(error))
                return
            if self.view.set_region(region) and self.tray.isSystemTrayAvailable():
                self.hidden_for_cast = True
                self.tray.show()
                return
        self.show_controls()
        if code == -1 or (code != 0 and error_output and "cancel" not in error_output.lower()):
            QMessageBox.information(self, "Select region", "Could not open the Wayland region selector.")

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_controls()

    def show_controls(self):
        self.hidden_for_cast = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def stop_cast(self):
        self.view.set_region(None)
        self.tray.hide()
        if self.hidden_for_cast:
            self.show_controls()

    def pairing_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(11)
        title = QLabel("Pair your phone")
        title.setFont(QFont("Inter", 24, QFont.Weight.DemiBold))
        center = Qt.AlignmentFlag.AlignHCenter
        layout.addWidget(title, 0, center)
        hint = QLabel("Open S-Pen Mirror on your phone.\nTap Pair PC to scan this code.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        data = subprocess.run(["qrencode", "-o", "-", "-t", "PNG", "-s", "5", "-m", "4"],
                              input=self.receiver.link.encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=5).stdout
        qr = QPixmap()
        if not qr.loadFromData(data, "PNG"):
            raise ValueError("Could not generate the pairing image.")
        image = QLabel()
        image.setPixmap(qr)
        image.setAccessibleName("Pairing QR code")
        layout.addWidget(image, 0, center)
        address = QLabel(f"{self.receiver.address}  ·  TCP {self.receiver.port}")
        address.setFont(QFont("monospace", 10))
        layout.addWidget(address, 0, center)
        self.copy = QPushButton("Copy pairing link")
        self.copy.clicked.connect(self.copy_link)
        layout.addWidget(self.copy, 0, center)
        self.copy_label_timer = QTimer(self, singleShot=True, interval=1800)
        self.copy_label_timer.timeout.connect(lambda: self.copy.setText("Copy pairing link"))
        self.clipboard_timer = QTimer(self, singleShot=True, interval=CLIPBOARD_TIMEOUT * 1000)
        self.clipboard_timer.timeout.connect(self.clear_pairing_clipboard)
        return page

    def copy_link(self):
        link = QMimeData()
        self.copied_link = self.receiver.issue_clipboard_link()
        link.setText(self.copied_link)
        link.setData("x-kde-passwordManagerHint", b"secret")
        QApplication.clipboard().setMimeData(link)
        self.copy.setText("Link copied")
        self.copy_label_timer.start()
        self.clipboard_timer.start()

    def clear_pairing_clipboard(self):
        self.clipboard_timer.stop()
        clipboard = QApplication.clipboard()
        if clipboard.text() == self.copied_link:
            clipboard.clear()
        self.copied_link = None

    def refresh(self):
        update = self.view.after(self.version)
        if update is None:
            return
        self.version, mode, image = update
        region = self.view.current_region()
        cast_status, _, _ = self.view.capture_update()
        self.surface.image = image
        self.surface.message = {
            "write": "Use the phone’s S-Pen to write over the active desktop window.",
            "cast": {
                CAST_READY: "The selected region is shared with your phone.",
                CAST_PAUSED: "Preview paused. Focus the selected display or choose a new region.",
                CAST_ERROR: "Could not capture the region. Try selecting it again.",
            }.get(cast_status, "Starting the phone preview…" if region
                else "Select a region, then draw on the phone’s live preview."),
        }.get(mode, "Waiting for the phone’s picture…")
        self.surface.update()
        self.annotation.set_enabled(mode == "write" or (mode == "cast" and region is not None))
        self.region_button.setVisible(mode == "cast")
        self.region_button.setText("Change region" if region else "Select region")
        self.stop_button.setVisible(mode == "cast" and region is not None)
        if mode == "cast" and region is None:
            self.tray.hide()
            if self.hidden_for_cast:
                self.show_controls()
        if mode != self.mode:
            if self.mode == "pair" and mode != "pair":
                self.clear_pairing_clipboard()
            self.mode = mode
            if mode != "cast":
                self.tray.hide()
                if self.selector is not None:
                    self.selector.kill()
                elif self.hidden_for_cast:
                    self.show_controls()
            self.stack.setCurrentIndex(0 if mode == "pair" else 1)
            self.status.setText({"pair": "Waiting for phone", "ink": "Live ink",
                                 "screen": "Screen mirror", "write": "Writing on desktop",
                                 "cast": "Write on PC"}[mode])
            self.save_button.setVisible(mode == "ink")
            self.full_button.setVisible(mode in ("ink", "screen"))
            if mode in ("pair", "cast"):
                self.leave_fullscreen()
        self.save_button.setEnabled(not image.isNull())

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        self.timer.stop()
        self.clear_pairing_clipboard()
        if self.selector is not None:
            selector, self.selector = self.selector, None
            selector.kill()
            selector.waitForFinished(200)
        self.capture.close()
        self.tray.hide()
        self.annotation.close()

    def save(self):
        image = QImage(self.surface.image)
        filename = "S-Pen " + datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".png"
        path, _ = QFileDialog.getSaveFileName(self, "Save ink canvas", filename, "PNG image (*.png)")
        if path and not image.save(path, "PNG"):
            QMessageBox.warning(self, "S-Pen Mirror", "Could not save the image to that location.")

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
        elif self.mode in ("ink", "screen"):
            self.showFullScreen()
            self.bar.hide()

    def leave_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.bar.show()
