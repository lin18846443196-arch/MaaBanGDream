from __future__ import annotations

import time
import traceback
import json

import cv2
import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import capture_image
except ImportError:  # AgentServer imports realtime as a top-level package.
    from foreground_guard import require_game_foreground
    from screen_refresh import capture_image

try:
    from .live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off
except ImportError:  # AgentServer 以顶层 realtime 包加载本模块时同样成立。
    from live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off


def formal_live_mode_is_off(image) -> bool:
    return live_performance_mode_is_off(image)


def cut_in_is_checked(image) -> bool:
    hsv = cv2.cvtColor(image[630:670, 480:520], cv2.COLOR_BGR2HSV)
    pink = (hsv[..., 1] >= 80) & (hsv[..., 2] >= 130)
    return float(np.count_nonzero(pink) / pink.size) > .12


def _preflight_params(raw) -> dict:
    """Legacy pipeline nodes omit parameters, which Maa serializes as null.

    Missing/null parameters retain all original single-player checks. Reject
    other JSON types before capturing or clicking; do not silently skip checks.
    """
    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("前置检查参数必须是 JSON 对象或 null")
    return raw


def _wait(context: Context, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if context.tasker.stopping:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(.1, remaining))


@AgentServer.custom_action("RealtimeFormalPreflight")
class RealtimeFormalPreflight(CustomAction):
    """Idempotently disable Auto Live, 3D Cut-in, and 3D/MV visuals."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _preflight_params(getattr(argv, 'custom_action_param', None))
            page_guard = params.get('page_guard')
            cut_in_off_node = params.get('cut_in_off_node')
            for _ in range(8):
                if context.tasker.stopping:
                    return True
                image = (capture_image(context, node=params.get('refresh_node', 'CommonRefreshScreen')) if page_guard else
                         context.tasker.controller.post_screencap().wait().get())
                if page_guard:
                    page = context.run_recognition(page_guard, image)
                    if not page or not page.hit:
                        raise RuntimeError('关闭 Cut in 前准备页已改变，停止输入')
                controller = context.tasker.controller
                auto = (context.run_recognition("AutoLiveEnabled", image)
                        if params.get('check_auto_live', True) else None)
                if auto and auto.hit and auto.box:
                    if context.tasker.stopping:
                        return True
                    box = auto.box
                    require_game_foreground(controller)
                    print(
                        "RealtimeFormalPreflight action=disable_auto_live "
                        f"target=({box.x + box.w // 2},{box.y + box.h // 2})",
                        flush=True,
                    )
                    controller.post_click(box.x + box.w // 2, box.y + box.h // 2).wait()
                    if not _wait(context, 1):
                        return False
                    continue
                # 3D 演出时 Cut-in 复选框尚未出现，同一区域显示的是成员头像；
                # 必须先把演出模式切到 OFF，再读取随后出现的 Cut-in 复选框。
                if params.get('check_performance_mode', True) and not formal_live_mode_is_off(image):
                    require_game_foreground(controller)
                    if context.tasker.stopping:
                        return True
                    print(
                        "RealtimeFormalPreflight action=cycle_performance_mode "
                        f"target={MODE_TOGGLE_POINT}",
                        flush=True,
                    )
                    controller.post_click(*MODE_TOGGLE_POINT).wait()
                    if not _wait(context, float(params.get('mode_settle_seconds', .5))):
                        return False
                    continue
                # Both states retain a check glyph: pink is ON, gray is OFF.
                # The optional gray template is positive OFF evidence, never
                # another reason to click an already disabled control.
                if cut_in_is_checked(image):
                    require_game_foreground(controller)
                    if context.tasker.stopping:
                        return True
                    print(
                        "RealtimeFormalPreflight action=disable_3d_cut_in "
                        "target=(500,650)",
                        flush=True,
                    )
                    controller.post_click(500, 650).wait()
                    if not _wait(context, float(params.get('cut_in_settle_seconds', 1))):
                        return False
                    continue
                if cut_in_off_node:
                    off = context.run_recognition(cut_in_off_node, image)
                    if context.tasker.stopping:
                        return True
                    if not off or not off.hit:
                        raise RuntimeError('未确认 Cut in 关闭状态，停止准备输入')
                print("RealtimeFormalPreflight completed=true", flush=True)
                return True
            raise RuntimeError("无法在正式演奏前关闭自动演出和演出显示效果")
        except Exception as exc:
            traceback.print_exc()
            print(f"RealtimeFormalPreflight failed={type(exc).__name__}: {exc}", flush=True)
            return False
