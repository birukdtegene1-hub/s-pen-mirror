"""Wayland region selection and bounded desktop capture."""

import re
import subprocess
import threading
import time
from dataclasses import dataclass


def monitor_bounds(monitor):
    scale = float(monitor["scale"])
    width, height = monitor["width"], monitor["height"]
    if monitor.get("transform", 0) in (1, 3, 5, 7):
        width, height = height, width
    return (monitor["x"], monitor["y"], round(width / scale), round(height / scale))


@dataclass(frozen=True)
class CastRegion:
    x: int
    y: int
    width: int
    height: int
    monitor: str
    bounds: tuple
    scale: float

    @classmethod
    def from_selection(cls, selection, monitors):
        match = re.fullmatch(r"(-?\d+),(-?\d+) (\d+)x(\d+)", selection.strip())
        if not match:
            raise ValueError("Drag to select a screen region.")
        x, y, width, height = map(int, match.groups())
        if width < 8 or height < 8:
            raise ValueError("Select an area at least 8 × 8 pixels.")
        candidates = []
        for monitor in monitors:
            bounds = monitor_bounds(monitor)
            left, top, span_x, span_y = bounds
            if (left - 1 <= x and x + width <= left + span_x + 1
                    and top - 1 <= y and y + height <= top + span_y + 1):
                start_x, start_y = max(x, left), max(y, top)
                crop_w = min(x + width, left + span_x) - start_x
                crop_h = min(y + height, top + span_y) - start_y
                if crop_w >= 8 and crop_h >= 8:
                    candidates.append(cls(start_x, start_y, crop_w, crop_h, monitor["name"], bounds,
                                          float(monitor["scale"])))
        if not candidates:
            raise ValueError("Select a region within one display.")
        return max(candidates, key=lambda region: region.width * region.height)

    def screenshot(self):
        scale = min(self.scale, 1920 / max(self.width, self.height))
        data = subprocess.check_output([
            "grim", "-g", f"{self.x},{self.y} {self.width}x{self.height}",
            "-s", str(scale), "-t", "jpeg", "-q", "87", "-",
        ], stderr=subprocess.DEVNULL, timeout=1.5)
        if not data.startswith(b"\xff\xd8") or len(data) > 8 * 1024 * 1024:
            raise ValueError("Invalid region image")
        return data

    def map_annotation(self, event):
        if "x" not in event:
            return event
        bx, by, bw, bh = self.bounds
        lx, ly = self.x - bx, self.y - by
        mapped = dict(event, x=(lx + event["x"] * (self.width - 1)) / max(1, bw - 1),
                      y=(ly + event["y"] * (self.height - 1)) / max(1, bh - 1))
        if event["action"] == "start":
            mapped.update(page_width=bw, page_height=bh,
                          clip=(lx / bw, ly / bh, self.width / bw, self.height / bh))
        return mapped


class RegionCapture:
    def __init__(self, view):
        self.view = view
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="desktop-capture", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()

    def _run(self):
        unchanged = 0
        while not self.closed.is_set():
            target = self.view.capture_target()
            if target is None:
                unchanged = 0
                self.closed.wait(0.08)
                continue
            revision, region = target
            start = time.monotonic()
            try:
                data = region.screenshot()
            except (OSError, subprocess.SubprocessError, ValueError):
                data = None
            changed = False
            if not self.closed.is_set():
                changed = self.view.publish_capture(revision, data)
            unchanged = 0 if changed or data is None else unchanged + 1
            interval = 0.7 if data is None else (1 / 6 if unchanged >= 4 else 1 / 12)
            self.closed.wait(max(0, interval - (time.monotonic() - start)))
