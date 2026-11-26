"""Session-owned control of Hyprland's desktop magnifier."""

import json
import logging
import math
import subprocess
from dataclasses import dataclass

from desktop.cast import hyprctl_ok

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZoomSettings:
    factor: float
    detached_camera: bool
    rigid: bool

    def matches(self, other):
        return (math.isclose(self.factor, other.factor, abs_tol=1e-5)
                and self.detached_camera == other.detached_camera and self.rigid == other.rigid)


ZOOM_OPTIONS = ("zoom_factor", "zoom_detached_camera", "zoom_rigid")


def current_zoom():
    lookups = ", ".join(f'tostring(hl.get_config("cursor.{option}"))' for option in ZOOM_OPTIONS)
    output = subprocess.run(["hyprctl", "repl", f'table.concat({{{lookups}}}, " ")'],
                            capture_output=True, timeout=0.4)
    if output.returncode == 0:
        parts = output.stdout.split()
        if len(parts) == 3 and all(part in (b"true", b"false", b"1", b"0") for part in parts[1:]):
            try:
                factor = float(parts[0])
            except ValueError:
                pass
            else:
                if 1 <= factor <= 10:
                    return ZoomSettings(factor, parts[1] in (b"true", b"1"), parts[2] in (b"true", b"1"))
    try:
        values = [json.loads(subprocess.check_output(
            ["hyprctl", "-j", "getoption", f"cursor:{option}"], stderr=subprocess.DEVNULL, timeout=0.4))
                  for option in ZOOM_OPTIONS]
        factor = float(values[0]["float"])
        if 1 <= factor <= 10:
            return ZoomSettings(factor, bool(values[1]["int"]), bool(values[2]["int"]))
    except (ValueError, KeyError, TypeError):
        pass
    raise OSError("Could not read Hyprland's magnifier settings")


def set_zoom(settings):
    factor = f"{settings.factor:.5f}"
    detached, rigid = ("true" if value else "false" for value in (settings.detached_camera, settings.rigid))
    lua = f"hl.config({{ cursor = {{ zoom_factor = {factor}, zoom_detached_camera = {detached}, zoom_rigid = {rigid} }} }})"
    if hyprctl_ok("eval", lua):
        return
    values = (factor, str(int(settings.detached_camera)), str(int(settings.rigid)))
    batch = " ; ".join(f"keyword cursor:{option} {value}" for option, value in zip(ZOOM_OPTIONS, values))
    output = subprocess.run(["hyprctl", "--batch", batch], capture_output=True, timeout=0.4)
    if output.returncode != 0 or output.stdout.split() != [b"ok"] * len(ZOOM_OPTIONS):
        raise OSError("Hyprland declined the zoom change")


class DesktopMagnifier:
    def __init__(self):
        self.original = None
        self.applied = None

    def change(self, steps):
        try:
            current = current_zoom()
            base = self.original if self.applied is not None and current.matches(self.applied) else current
            level = round(max(1, min(10, current.factor * (1.25 ** steps))), 5)
            # A cursor-anchored camera has an invertible transform for pen input in the cast.
            target = ZoomSettings(level, False, False) if level > 1 else ZoomSettings(1, base.detached_camera, base.rigid)
            if not target.matches(current):
                set_zoom(target)
                self.original, self.applied = base, target
            return level
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("Could not change desktop zoom: %s", error)
            return None

    def close(self):
        try:
            if (self.applied is not None and not self.applied.matches(self.original)
                    and current_zoom().matches(self.applied)):
                set_zoom(self.original)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("Could not restore desktop zoom: %s", error)
        finally:
            self.original = self.applied = None
