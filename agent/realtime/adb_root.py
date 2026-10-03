"""Acquire emulator ADB root before a room is joined, with bounded probes."""
from __future__ import annotations

from collections.abc import Callable
import subprocess
import time

from .native_prearm import controller_adb_endpoint


def enable_adb_root(
    controller, *, stopping: Callable[[], bool], timeout_seconds: float = 15.0,
) -> tuple[bool, str]:
    if stopping():
        raise InterruptedError("用户已停止任务")
    try:
        adb_path, serial = controller_adb_endpoint(controller)
    except RuntimeError as exc:
        return False, f"无法获取当前 ADB 连接：{exc}"
    deadline = time.monotonic() + timeout_seconds

    def run(*args: str):
        if stopping():
            raise InterruptedError("用户已停止任务")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ADB root 检查超时")
        return subprocess.run(
            [adb_path, "-s", serial, *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=min(4.0, remaining),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    try:
        result = run("root")
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        return False, f"ADB root 启用失败：{type(exc).__name__}: {exc}"
    reply = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or "cannot run as root" in reply.lower():
        return False, f"模拟器拒绝 ADB root：{reply[:240] or result.returncode}"

    last_reply = reply
    # adbd may restart its transport. Fresh CLI probes wait for that transport;
    # do not recreate Maa's controller or replace its active callback handle.
    while time.monotonic() < deadline:
        try:
            result = run("shell", "id", "-u")
        except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
            last_reply = f"{type(exc).__name__}: {exc}"
        else:
            if result.returncode == 0 and result.stdout.strip() == "0":
                if stopping():
                    raise InterruptedError("用户已停止任务")
                return True, "当前 ADB 连接已确认 root UID=0"
            last_reply = (result.stdout + result.stderr).strip()
        if stopping():
            raise InterruptedError("用户已停止任务")
        time.sleep(max(0.0, min(.25, deadline - time.monotonic())))
    return False, f"ADB root 后未能确认 root 权限：{last_reply[:240]}"
