import datetime
import subprocess

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)


class Surface(QWidget):
    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.setMinimumSize(160, 180)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), QColor("#222927"))
            if self.image.isNull():
                painter.setPen(QColor("#d3dcd5"))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for the phone’s picture…")
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
        self.receiver, self.view = receiver, view
        self.version = -1
        self.mode = ""
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
        self.surface = Surface()
        self.stack.addWidget(self.surface)
        self.full_shortcut = QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        self.escape = QShortcut(QKeySequence("Escape"), self, activated=self.leave_fullscreen)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

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
        return page

    def copy_link(self):
        QApplication.clipboard().setText(self.receiver.link)
        self.copy.setText("Link copied")
        QTimer.singleShot(1800, lambda: self.copy.setText("Copy pairing link"))

    def refresh(self):
        update = self.view.after(self.version)
        if update is None:
            return
        self.version, mode, image = update
        self.surface.image = image
        self.surface.update()
        if mode != self.mode:
            self.mode = mode
            self.stack.setCurrentIndex(0 if mode == "pair" else 1)
            self.status.setText({"pair": "Waiting for phone", "ink": "Live ink", "screen": "Screen mirror"}[mode])
            self.save_button.setVisible(mode == "ink")
            self.full_button.setVisible(mode != "pair")
            if mode == "pair":
                self.leave_fullscreen()
        self.save_button.setEnabled(not image.isNull())

    def save(self):
        image = QImage(self.surface.image)
        filename = "S-Pen " + datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S") + ".png"
        path, _ = QFileDialog.getSaveFileName(self, "Save ink canvas", filename, "PNG image (*.png)")
        if path and not image.save(path, "PNG"):
            QMessageBox.warning(self, "S-Pen Mirror", "Could not save the image to that location.")

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.leave_fullscreen()
        elif self.mode != "pair":
            self.showFullScreen()
            self.bar.hide()

    def leave_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.bar.show()
