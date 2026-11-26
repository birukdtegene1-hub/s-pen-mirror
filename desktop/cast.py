"""Wayland region selection, monitor switching and bounded desktop capture."""

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


def monitor_bounds(monitor):
    scale = float(monitor["scale"])
    width, height = monitor["width"], monitor["height"]
    if monitor.get("transform", 0) in (1, 3, 5, 7):
        width, height = height, width
    return (monitor["x"], monitor["y"], round(width / scale), round(height / scale))


def available_monitor(monitor):
    return monitor.get("dpmsStatus", True) and monitor.get("mirrorOf") in (None, "", "none")


def adjacent_monitor(monitors, source, direction):
    origin = next((monitor for monitor in monitors if monitor["name"] == source), None)
    if origin is None:
        origin = next((monitor for monitor in monitors if monitor.get("focused")), None)
    if origin is None:
        return None
    x, y, width, height = monitor_bounds(origin)
    candidates = []
    for monitor in monitors:
        if monitor is origin or not available_monitor(monitor):
            continue
        left, top, span_x, span_y = monitor_bounds(monitor)
        dx = direction * (left + span_x / 2 - x - width / 2)
        dy = abs(top + span_y / 2 - y - height / 2)
        overlap = min(y + height, top + span_y) - max(y, top)
        if dx > 0 and (overlap > 0 or dx >= dy):
            candidates.append(((overlap <= 0, dx, dy, monitor["id"]), monitor))
    return min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def hyprctl_ok(*arguments):
    result = subprocess.run(["hyprctl", *arguments], capture_output=True, timeout=0.4)
    return result.returncode == 0 and result.stdout.strip() == b"ok"


def dispatch_focus(modern, legacy, *arguments):
    return hyprctl_ok("dispatch", f"hl.dsp.focus({{ {modern} }})") or hyprctl_ok("dispatch", legacy, *arguments)


def focus_adjacent_monitor(source, direction):
    try:
        monitors = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"],
                                                      stderr=subprocess.DEVNULL, timeout=0.4))
        available = [monitor for monitor in monitors if available_monitor(monitor)]
        if len(available) == 1:
            current = available[0].get("activeWorkspace", {}).get("id")
            if isinstance(current, int) and current > 0 and 1 <= current + direction <= 2147483647:
                workspace = current + direction
                dispatch_focus(f"workspace = {workspace}, on_current_monitor = true", "workspace", str(workspace))
            return None
        target = adjacent_monitor(available, source, direction)
        if target is None:
            return None
        monitor_id = int(target["id"])
        if dispatch_focus(f"monitor = {monitor_id}", "focusmonitor", str(monitor_id)):
            return target
        log.warning("Hyprland declined to focus display %s", target["name"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, ZeroDivisionError) as error:
        log.warning("Could not switch the shared monitor: %s", error)
    return None


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
    def from_monitor(cls, monitor):
        bounds = monitor_bounds(monitor)
        return cls(*bounds, monitor["name"], bounds, float(monitor["scale"]))

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
            "grim", "-c", "-g", f"{self.x},{self.y} {self.width}x{self.height}",
            "-s", str(scale), "-t", "jpeg", "-q", "87", "-",
        ], stderr=subprocess.DEVNULL, timeout=1.5)
        if not data.startswith(b"\xff\xd8") or len(data) > 8 * 1024 * 1024:
            raise ValueError("Invalid region image")
        return data

    def zoom_transform(self, factor):
        if factor <= 1:
            return None
        cursor = json.loads(subprocess.check_output(["hyprctl", "-j", "cursorpos"],
                                                    stderr=subprocess.DEVNULL, timeout=0.4))
        cx, cy = float(cursor["x"]) - self.bounds[0], float(cursor["y"]) - self.bounds[1]
        if 0 <= cx < self.bounds[2] and 0 <= cy < self.bounds[3]:
            return factor, cx, cy
        return None

    def map_annotation(self, event, transform=None):
        if "x" not in event:
            return event
        bx, by, bw, bh = self.bounds
        lx, ly = self.x - bx, self.y - by
        factor = 1
        if transform is not None:
            factor, cx, cy = transform
            lx = cx + (lx - cx) / factor
            ly = cy + (ly - cy) / factor
        mapped = dict(event, x=(lx + event["x"] * (self.width - 1) / factor) / max(1, bw - 1),
                      y=(ly + event["y"] * (self.height - 1) / factor) / max(1, bh - 1))
        if event["action"] == "start":
            mapped.update(page_width=bw, page_height=bh,
                          clip=(lx / bw, ly / bh, self.width / (bw * factor), self.height / (bh * factor)))
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
            factor = self.view.zoom_at(revision)
            if factor is None:
                continue
            transform = None
            try:
                transform = region.zoom_transform(factor)
                data = region.screenshot()
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                data = None
            changed = False
            if not self.closed.is_set():
                changed = self.view.publish_capture(revision, data, transform)
            unchanged = 0 if changed or data is None else unchanged + 1
            interval = 0.7 if data is None else (1 / 6 if unchanged >= 4 else 1 / 12)
            self.closed.wait(max(0, interval - (time.monotonic() - start)))
