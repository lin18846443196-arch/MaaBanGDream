"""Keep MuMu's renderer IPC away from portrait launch/escape transitions."""
from __future__ import annotations

import re
import time

try:
    from .foreground_guard import (
        GAME_PACKAGE, _parse_foreground, game_foreground_display, mumu_extras_active,
    )
    from .maa_shell_compat import shell_output
except ImportError:
    from foreground_guard import (
        GAME_PACKAGE, _parse_foreground, game_foreground_display, mumu_extras_active,
    )
    from maa_shell_compat import shell_output


def _landscape_display_size(output: str, display_id: int) -> tuple[int, int] | None:
    # BaseDisplayInfo describes the physical panel, even when the app rotates.
    # Only OverrideDisplayInfo supplies the logical dimensions we will capture.
    pattern = (r"mOverrideDisplayInfo\s*=\s*DisplayInfo\{[^\r\n]*?displayId\s+"
               r"(\d+)[^\r\n]*?real\s+(\d+)\s+x\s+(\d+)")
    for match in re.finditer(pattern, str(output)):
        current, width, height = (int(value) for value in match.groups())
        if current == display_id:
            return (width, height) if width > height > 0 else None
    return None


def display_is_landscape(output: str, display_id: int) -> bool:
    return _landscape_display_size(output, display_id) is not None


def wait_for_game_capture_ready(
    context, package: str = GAME_PACKAGE, *, timeout_seconds: float = 15.0,
    stable_seconds: float = .5,
) -> bool:
    """Poll ADB only, then allow capture once the game display is stable.

    MuMu SDK can crash the host while reconnecting its frame buffer after a
    portrait desktop screenshot. This check runs only at app transitions;
    it adds no queries to the real-time song capture path.
    """
    if not mumu_extras_active(context.tasker.controller):
        return True
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last_geometry = None
    last_error = "game display is not ready"
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            raise InterruptedError("用户已停止任务")
        try:
            controller = context.tasker.controller
            window = shell_output(controller, "dumpsys window", 3000)
            activity = shell_output(controller, "dumpsys activity activities", 3000)
            display_id = game_foreground_display(window, activity, package)
            if display_id is None and _parse_foreground(window) == package:
                top = re.search(r"mTopFocusedDisplayId\s*=\s*(\d+)", window)
                # Virtual panels always require the resumed-activity evidence.
                if top is not None and int(top.group(1)) == 0:
                    display_id = 0
            dimensions = shell_output(controller, "dumpsys display", 3000)
            size = _landscape_display_size(dimensions, display_id) if display_id is not None else None
            ready = size is not None
            geometry = (display_id, size)
            last_error = f"game_display={display_id}, landscape={ready}"
        except Exception as exc:
            ready = False
            last_error = f"{type(exc).__name__}: {exc}"
        if ready:
            if stable_since is None or last_geometry != geometry:
                stable_since = time.monotonic()
            last_geometry = geometry
            if time.monotonic() - stable_since >= stable_seconds:
                print(f"MuMuCaptureTransition ready=true display={display_id}", flush=True)
                return True
        else:
            stable_since = None
            last_geometry = None
        time.sleep(.15)
    raise RuntimeError("MuMu 游戏横屏窗口未稳定，已暂停截图以避免原生闪退：" + last_error)
