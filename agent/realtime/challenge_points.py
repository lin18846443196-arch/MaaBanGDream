from __future__ import annotations

import json
import re
import time
import traceback
import unicodedata
from datetime import datetime

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

try:
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import ScreenRefreshCancelled, capture_image
    from ..task_reporting import log_task, record_failure_reason
except ImportError:
    from foreground_guard import require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import log_task, record_failure_reason

from .live_session import current_live_run
from .difficulty_action import _digit_shape_scores
from .profile_action import PROJECT_ROOT
from .runtime_options import debug_enabled, diagnostic_trace_enabled
from .song_title_ocr import recognize_song_title
from .vision_io import imwrite_unicode


POINTS_ROI = (640, 126, 198, 28)
MULTIPLIERS = (8, 4, 2, 1)
BASE_COST = 200
MULTIPLIER_TARGETS = {1: (876, 212), 2: (876, 285), 4: (876, 358), 8: (876, 430)}
CONFIRM_POINT = (770, 620)
CANCEL_POINT = (510, 620)


def parse_points(text: str) -> int | None:
    value = unicodedata.normalize("NFKC", text).strip()
    # The bundled CTC dictionary retains quotes around ASCII digit tokens.
    value = value.replace("'", "").replace('"', "")
    if not re.fullmatch(r"(?:[0-9]{1,8}|[0-9]{1,3}(?:,[0-9]{3}){1,2})", value):
        return None
    return int(value.replace(",", ""))


def read_points(image: np.ndarray) -> int | None:
    if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
        return None
    x, y, width, height = POINTS_ROI
    gray = cv2.cvtColor(image[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(gray < 170)
    if len(xs) < 12:
        return None
    left, top = max(0, int(xs.min()) - 3), max(0, int(ys.min()) - 3)
    right, bottom = min(width, int(xs.max()) + 4), min(height, int(ys.max()) + 4)
    reading = recognize_song_title(image, roi=(x + left, y + top, right - left, bottom - top))
    if reading is None:
        return None
    points = parse_points(reading.text)
    if reading.confidence >= 0.90:
        return points
    # A lone zero has low CTC confidence; require independent glyph evidence.
    if points == 0:
        scores = _digit_shape_scores(np.where(gray < 170, 255, 0).astype(np.uint8))
        if (len(scores) >= 2 and scores[0][1] == 0 and scores[0][0] < 0.25
                and scores[1][0] - scores[0][0] > 0.10):
            return 0
    return None


def affordable_multiplier(points: int) -> int | None:
    return next((value for value in MULTIPLIERS if points >= value * BASE_COST), None)


def selected_multiplier(image: np.ndarray) -> int | None:
    if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
        return None
    selected = []
    for multiplier, (x, y) in MULTIPLIER_TARGETS.items():
        hsv = cv2.cvtColor(image[y - 10:y + 11, x - 10:x + 11], cv2.COLOR_BGR2HSV)
        pink = (((hsv[..., 0] <= 12) | (hsv[..., 0] >= 165))
                & (hsv[..., 1] >= 65) & (hsv[..., 2] >= 130))
        if float(np.count_nonzero(pink) / pink.size) >= 0.25:
            selected.append(multiplier)
    return selected[0] if len(selected) == 1 else None


def wait(context, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            raise ScreenRefreshCancelled("task is stopping")
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def click(context, point: tuple[int, int]) -> None:
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task is stopping")
    controller = context.tasker.controller
    require_game_foreground(controller)
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task is stopping")
    if not controller.post_click(*point).wait().succeeded:
        raise RuntimeError("挑战点数点击失败")


def point_dialog(context, image) -> bool:
    detail = context.run_recognition("ChallengePointMarker", image)
    return bool(detail and detail.hit)


@AgentServer.custom_recognition("ChallengeSongPageVisible")
class ChallengeSongPageVisible(CustomRecognition):
    def analyze(self, context, argv):
        if context.tasker.stopping:
            return None
        dialog = context.run_recognition("ChallengePointMarker", argv.image)
        if dialog is None or dialog.hit:
            return None
        for node in ("ChallengeSongHeaderMarker", "ChallengeSongPageMarker"):
            detail = context.run_recognition(node, argv.image)
            if detail is None or not detail.hit:
                return None
        return (110, 10, 350, 80)


def evidence(image, *, points: int | None, multiplier: int | None, reason: str = "") -> None:
    if not reason and not (debug_enabled() or diagnostic_trace_enabled()):
        return
    try:
        output = PROJECT_ROOT / "screencap"
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = output / f"challenge-points-{stamp}"
        run = current_live_run()
        if isinstance(image, np.ndarray):
            imwrite_unicode(base.with_suffix(".png"), image)
        base.with_suffix(".json").write_text(json.dumps({
            "mode": "challenge", "run_id": None if run is None else run.run_id,
            "points": points, "multiplier": multiplier,
            "cost": None if multiplier is None else multiplier * BASE_COST,
            "reason": reason,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"ChallengePoints evidence_warning={exc}", flush=True)


@AgentServer.custom_action("ChallengePointsSelect")
class ChallengePointsSelect(CustomAction):
    def run(self, context, argv) -> bool:
        image = None
        points = multiplier = None
        try:
            if context.tasker.stopping:
                return True
            if not context.override_next(argv.node_name, ["ChallengePointConfirm"]):
                raise RuntimeError("挑战点数流程分支初始化失败")
            previous = None
            for _ in range(8):
                image = capture_image(context)
                if not point_dialog(context, image):
                    raise RuntimeError("挑战点数弹窗已改变，停止输入")
                points = read_points(image)
                if points is not None and points == previous:
                    break
                previous = points
                wait(context, 0.25)
            else:
                raise RuntimeError("无法连续确认所持挑战点数，停止开演")
            multiplier = affordable_multiplier(points)
            if multiplier is None:
                if not context.override_next(argv.node_name, ["ChallengePointsExhaustedRecover"]):
                    raise RuntimeError("挑战点数耗尽分支设置失败")
                evidence(image, points=points, multiplier=None)
                log_task("挑战演出", "点数", "INFO", f"所持 {points} 点，不足 200 点，正常结束")
                click(context, CANCEL_POINT)
                return True
            for _ in range(3):
                if selected_multiplier(image) == multiplier:
                    evidence(image, points=points, multiplier=multiplier)
                    log_task("挑战演出", "点数", "INFO",
                             f"所持 {points} 点，选择 {multiplier} 倍，消耗 {multiplier * BASE_COST} 点")
                    return True
                click(context, MULTIPLIER_TARGETS[multiplier])
                wait(context, 0.25)
                image = capture_image(context)
                if not point_dialog(context, image) or read_points(image) != points:
                    raise RuntimeError("选择倍率时挑战点数或弹窗发生变化，停止开演")
            if selected_multiplier(image) != multiplier:
                raise RuntimeError("挑战倍率选中状态未确认，停止开演")
            evidence(image, points=points, multiplier=multiplier)
            return True
        except ScreenRefreshCancelled:
            return True
        except Exception as exc:
            reason = f"挑战点数选择失败：{exc}"
            record_failure_reason(reason)
            evidence(image, points=points, multiplier=multiplier, reason=reason)
            traceback.print_exc()
            return False


@AgentServer.custom_action("ChallengePointsConfirm")
class ChallengePointsConfirm(CustomAction):
    def run(self, context, argv) -> bool:
        image = None
        points = multiplier = None
        try:
            image = capture_image(context)
            if not point_dialog(context, image):
                raise RuntimeError("挑战点数弹窗已关闭，停止确认")
            points = read_points(image)
            if points is None:
                raise RuntimeError("确认前无法读取挑战点数")
            multiplier = affordable_multiplier(points)
            if multiplier is None or selected_multiplier(image) != multiplier:
                raise RuntimeError("确认前余额或选中倍率不一致")
            click(context, CONFIRM_POINT)
            return True
        except ScreenRefreshCancelled:
            return True
        except Exception as exc:
            reason = f"挑战点数确认失败：{exc}"
            record_failure_reason(reason)
            evidence(image, points=points, multiplier=multiplier, reason=reason)
            traceback.print_exc()
            return False
