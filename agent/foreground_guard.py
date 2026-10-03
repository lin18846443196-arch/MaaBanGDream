from __future__ import annotations

import re
import subprocess
from typing import Protocol

try:
    from .maa_shell_compat import shell_output
except ImportError:
    from maa_shell_compat import shell_output


GAME_PACKAGE = "com.bilibili.star.bili"
_PACKAGE_RE = re.compile(r"\s([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+")
_DISPLAY_RE = re.compile(r"^\s*Display:\s*mDisplayId=(\d+)")
_FOCUS_RE = re.compile(r"^\s*(mCurrentFocus|mFocusedApp)=(.*)$", re.MULTILINE)
_TOP_DISPLAY_RE = re.compile(r"\bmTopFocusedDisplayId=(\d+)")
_ACTIVITY_DISPLAY_RE = re.compile(r"^Display #(\d+) \(activities from top to bottom\):")
_RESUMED_RE = re.compile(r"^\s*(?:topResumedActivity=|Resumed:\s*)(.*)$")


class _Job(Protocol):
    def wait(self) -> "_Job": ...
    def get(self) -> object: ...


class _Controller(Protocol):
    def post_shell(self, command: str, timeout: int = 20000) -> _Job: ...


class ForegroundAppMismatch(RuntimeError):
    pass


def _package(value: str) -> str | None:
    match = _PACKAGE_RE.search(value)
    return match.group(1) if match else None


def _display_focuses(output: object) -> dict[int, dict[str, str | None]]:
    displays: dict[int, dict[str, str | None]] = {}
    current_display: int | None = None
    for line in str(output or "").splitlines():
        display = _DISPLAY_RE.match(line)
        if display:
            current_display = int(display.group(1))
            displays[current_display] = {}
            continue
        if line.startswith("  WINDOW MANAGER "):
            current_display = None
        focus = _FOCUS_RE.match(line)
        if current_display is not None and focus:
            displays[current_display].setdefault(focus.group(1), _package(focus.group(2)))
    return displays


def _current_focus(focus: dict[str, str | None]) -> str | None:
    # A known null window is not proof that the remembered activity is focused.
    if "mCurrentFocus" in focus:
        return focus["mCurrentFocus"]
    return focus.get("mFocusedApp")


def _parse_foreground(output: object) -> str | None:
    text = str(output or "")
    displays = _display_focuses(text)
    if displays:
        top = _TOP_DISPLAY_RE.search(text)
        if top:
            return _current_focus(displays.get(int(top.group(1)), {}))
        if len(displays) == 1:
            return _current_focus(next(iter(displays.values())))
        return None
    matches = {match.group(1): _package(match.group(2)) for match in _FOCUS_RE.finditer(text)}
    return _current_focus(matches)


def _resumed_displays(output: object) -> dict[int, set[str]]:
    resumed: dict[int, set[str]] = {}
    current_display: int | None = None
    for line in str(output or "").splitlines():
        display = _ACTIVITY_DISPLAY_RE.match(line)
        if display:
            current_display = int(display.group(1))
            resumed[current_display] = set()
            continue
        if line.startswith("  ResumedActivity:") or line.startswith("  ActivityTaskSupervisor state:"):
            current_display = None
        activity = _RESUMED_RE.match(line)
        if current_display is not None and activity:
            package = _package(activity.group(1))
            if package:
                resumed[current_display].add(package)
    return resumed


def mumu_extras_active(controller: _Controller) -> bool:
    try:
        info = controller.info
        enabled = info["config"]["extras"]["mumu"]["enable"] is True
        return enabled and bool(int(info["screencap_methods"]) & 64)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def game_foreground_display(
    window: object, activities: object, package: str = GAME_PACKAGE
) -> int | None:
    """Return a uniquely resumed game panel confirmed by its display focus."""
    displays = _display_focuses(window)
    resumed = _resumed_displays(activities)
    matches: list[int] = []
    for display, packages in resumed.items():
        if display == 0 or packages != {package}:
            continue
        focus = displays.get(display, {})
        if "mCurrentFocus" not in focus:
            continue
        current = focus["mCurrentFocus"]
        if current == package:
            matches.append(display)
        elif current is None and focus.get("mFocusedApp") == package:
            # MuMu's app panel can be resumed while the main desktop owns the
            # global focus. Both independent dumps must identify the same panel.
            matches.append(display)
    return matches[0] if len(matches) == 1 else None


def _parse_mumu_foreground(window: str, activities: str) -> str | None:
    if game_foreground_display(window, activities) is not None:
        return GAME_PACKAGE
    return _parse_foreground(window)


def _read_dump(controller: _Controller, *command: str) -> str:
    try:
        output = shell_output(controller, " ".join(command), 5000)
        if output:
            return output
    except Exception:
        pass
    try:
        info = controller.info
        completed = subprocess.run(
            [str(info["adb_path"]), "-s", str(info["adb_serial"]), "shell", *command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.stdout
    except Exception:
        return ""


def foreground_package(controller: _Controller) -> str | None:
    window = _read_dump(controller, "dumpsys", "window")
    if mumu_extras_active(controller):
        activities = _read_dump(controller, "dumpsys", "activity", "activities")
        return _parse_mumu_foreground(window, activities)
    package = _parse_foreground(window)
    if package:
        return package
    if _display_focuses(window) or _FOCUS_RE.search(window):
        return None
    return _parse_foreground(_read_dump(controller, "dumpsys", "window", "windows"))


def require_game_foreground(controller: _Controller, package: str = GAME_PACKAGE) -> None:
    actual = foreground_package(controller)
    if actual != package:
        raise ForegroundAppMismatch(
            f"unsafe controller input blocked: expected foreground={package}, actual={actual or 'unknown'}"
        )
