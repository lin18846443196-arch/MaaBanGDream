from __future__ import annotations

import json
import re
import threading
import time
import traceback
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..capture_transition import wait_for_game_capture_ready
    from ..foreground_guard import GAME_PACKAGE, foreground_package, require_game_foreground
    from ..screen_refresh import ScreenRefreshCancelled, capture_image
    from ..task_reporting import TaskProgress, latest_failure_reason, record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from capture_transition import wait_for_game_capture_ready
    from foreground_guard import GAME_PACKAGE, foreground_package, require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import TaskProgress, latest_failure_reason, record_failure_reason

from .difficulty_action import RealtimeDifficultySelect
from .game_effect_settings_action import _click as _maa_click
from .vision_io import imread_unicode, imwrite_unicode
from .game_effect_settings_action import _swipe as _maa_swipe
from .live_session import (
    append_current_run_event,
    current_live_run,
    update_live_run,
)
from .life_monitor import LifeDetector, LifeGuard, LifeStatus
from .live_visual_gate import MODE_TOGGLE_POINT, live_performance_mode_is_off
from .performance_settings_action import RealtimePerformanceSettingsGate
from .playfield_monitor import PlayfieldDetector
from .chart_repository import LocalChartRepository
from .final_cover import FinalCoverResolver, cooperative_member_loading_guard_enabled
from .profile_play_action import RealtimeProfilePlay
from .profile_store import (
    EnvironmentSignature,
    RealtimeProfileStore,
    engine_from_native_flag,
)
from .rehearsal_action import frame_resolution
from .native_prearm import discard_prearmed_backend
from .cooperative_network import GameNetworkGate
from .adb_root import enable_adb_root
from .result_navigation import (
    RESULT_ANIMATION_SKIP_POINT,
    accelerated_back,
    advance_result_cadence,
    handle_story_page,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "resource" / "image" / "cooperative"
# 与 prepare() 传给流速门禁的环境参数保持一致：这些值同时用于开局前的
# Profile 预检，避免两处硬编码漂移。
COOPERATIVE_DPI = 240
COOPERATIVE_GAME_FPS = 60
COOPERATIVE_RENDER_QUALITY = "standard"
TEMPLATE_POSITIONS = {
    "live_entry": (975, 448),
    "room_search": (565, 620),
    "search_private": (545, 493),
    "search_friend": (765, 493),
    "friend_invite_title": (175, 78),
    "private_room_title": (392, 210),
    "room_wait": (110, 58),
    "song_unspecified": (690, 612),
    "song_random": (690, 528),
    "ready_button": (1010, 575),
    "member_exit_title": (399, 158),
    "connect_failed_body": (580, 345),
    "connect_failed_retry": (660, 500),
    "repeat_room_title": (393, 225),
    "sss_guide_close": (856, 610),
    "result_replay": (707, 618),
    "result_confirm": (959, 618),
    # 断网跳车：真实弹窗正文（2026-09-07 雷电录像提取）。
    "disconnect_continue_body": (488, 313),
    "disconnect_confirm_body": (495, 307),
    "disconnect_confirm_body_current": (475, 298),
    "restriction_body": (300, 257),
    "restriction_ok": (527, 460),
}

DEFAULT_SETTINGS: dict[str, object] = {
    "entry_method": "normal",
    "room_tier": "free",
    "room_code": "",
    "difficulty": "Expert",
    "count": 1,
    "post_live_action": "exit",
    "member_exit_policy": "fail",
    "max_reconnects": 3,
    "debug_recording": False,
    "diagnostic_trace": True,
    "disconnect_jump_enabled": False,
    "song_choice": "unspecified",
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_SETTINGS_LOCK = threading.Lock()

ROOM_TIER_INDEX = {
    "free": 0,
    "beginner": 1,
    "chief": 2,
    "legend": 3,
}
COOPERATIVE_DIFFICULTY_TARGETS = {
    "Easy": (602, 575),
    "Normal": (687, 575),
    "Hard": (769, 575),
    "Expert": (852, 575),
    "Special": (942, 575),
}
MEMBER_DOWNLOAD_TIMEOUT_SECONDS = 60.0
ROOM_SONG_CHOICE_TIMEOUT_SECONDS = 180.0
SONG_CHOICE_TO_READY_TIMEOUT_SECONDS = 60.0
POST_SCORE_NAVIGATION_TIMEOUT_SECONDS = 60.0
HOME_LIVE_POINT = (1175, 645)
# 断网跳车按钮点击点：弹窗1“通信已中断。是否继续演出？”点左侧“中断”；
# 弹窗2“确认中断当前演出返回主页吗？”点右侧粉色“中断”。
DISCONNECT_CONTINUE_INTERRUPT_POINT = (508, 447)
DISCONNECT_CONFIRM_INTERRUPT_POINT = (754, 439)
READY_DELIVERY_OBSERVE_SECONDS = 2.0
# 选曲页坐标沿用贡献者的 1280×720 标定；默认仍选择不指定歌曲。
COOPERATIVE_SONG_RANDOM_POINT = (782, 565)
COOPERATIVE_SONG_UNSPECIFIED_POINT = (780, 647)
COOPERATIVE_SONG_CONFIRM_POINT = (1068, 647)
COOPERATIVE_SONG_CHOICES = ("unspecified", "random", "current")
COOPERATIVE_SONG_CHOICE_PAUSE_SECONDS = 10.0


@dataclass
class ConnectRetryState:
    """A navigation-wide budget, including dialogs that disappear and recur."""

    limit: int = 5
    clicks: int = 0
    next_click_at: float = 0.0
    unclickable_since: float | None = None


def _frame_is_black_transition(image: np.ndarray) -> bool:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 6.0 and float(gray.std()) < 6.0


class CooperativePlayfieldEntryEvidence:
    """为准备后漏黑场保留的有界演奏场动态证据。

    PlayfieldDetector 的生命条和白色判定线在准备页也可能同时出现，不能单独
    放行。这里要求真实音符头、连续两帧局部列变化，并排除成员准备弹窗及
    大面积转场/变暗。它只决定成员退出监听何时结束，不参与
    Native 首音锚点、scheduler 或任何歌曲时间计算。
    """

    _REFERENCE_HEIGHT = 720
    _TOP = 430
    _BOTTOM = 570
    _COLUMN_CHANGE_THRESHOLD = 18.0
    _MIN_CHANGED_COLUMNS = 12
    _BROAD_FRACTION = 0.30
    _REQUIRED_NARROW_EVENTS = 2

    def __init__(self) -> None:
        from .first_note_evidence import has_approaching_note_head
        from .prepare_popup import CooperativePreparePopupDetector

        self._note_head_detector = has_approaching_note_head
        self._prepare_popup_detector = CooperativePreparePopupDetector(verify_content=True)
        self._previous_columns: np.ndarray | None = None
        self._narrow_motion_streak = 0

    def reset(self) -> None:
        self._previous_columns = None
        self._narrow_motion_streak = 0

    def observe(self, image: np.ndarray, *, playfield_visible: bool) -> bool:
        if not playfield_visible:
            self.reset()
            return False
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] < 3
            or image.shape[0] < 2
            or image.shape[1] < 2
        ):
            self.reset()
            return False
        if self._prepare_popup_detector(image) or not self._note_head_detector(image):
            # Stage lighting can produce consecutive narrow changes too.
            # Retain only motion observed while an actual note head is visible;
            # this is exit evidence, never permission to anchor mid-song.
            self.reset()
            return False
        height = image.shape[0]
        scale = height / self._REFERENCE_HEIGHT
        top = max(0, min(height - 1, round(self._TOP * scale)))
        bottom = max(top + 1, min(height, round(self._BOTTOM * scale)))
        columns = image[top:bottom, :, :3].astype("float32").mean(axis=(0, 2))
        if self._previous_columns is None:
            self._previous_columns = columns
            return False
        changed_columns = int(np.count_nonzero(
            np.abs(columns - self._previous_columns)
            >= self._COLUMN_CHANGE_THRESHOLD
        ))
        self._previous_columns = columns
        if changed_columns >= image.shape[1] * self._BROAD_FRACTION:
            # 整屏淡入、成员等待弹窗及其遮罩都会造成大面积同向变化，不是音符。
            self._narrow_motion_streak = 0
            return False
        if changed_columns < self._MIN_CHANGED_COLUMNS:
            self._narrow_motion_streak = 0
            return False
        self._narrow_motion_streak += 1
        return self._narrow_motion_streak >= self._REQUIRED_NARROW_EVENTS


def cooperative_play_params(
    settings: dict[str, object],
    *,
    effective_difficulty: str | None = None,
) -> dict[str, object]:
    return {
        "difficulty": str(effective_difficulty or settings["difficulty"]),
        "require_profile": True,
        "settings_gate_required": True,
        "debug_recording": bool(settings["debug_recording"]),
        "diagnostic_trace": bool(settings["diagnostic_trace"]),
        "duration_seconds": 600,
        "startup_timeout_seconds": 60,
        "dpi": 240,
        "game_fps": 60,
        "render_quality": "standard",
        "wait_for_completion": True,
        "completion_missing_frames": 30,
        "require_completion": True,
        "save_result_frame": True,
        "result_back_attempts": 30,
        "result_back_interval_seconds": 1.5,
        "continue_after_life_depleted": True,
        "run_mode": "cooperative",
        "confirm_final_cover": True,
        "final_cover_timeout_seconds": MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
        "native_prearm_deferred": True,
        "life_depleted_jump_request": bool(
            settings.get("disconnect_jump_enabled", False)
        ),
    }


class MemberExited(RuntimeError):
    pass


class JumpOutUnavailable(RuntimeError):
    """协力局已安全跳车或无法继续自动恢复，应立即结束任务。"""


class CooperativeStartupRetry(RuntimeError):
    """An invalid startup was exited safely; retry through the bounded loop."""


def configure_cooperative_settings(params: dict[str, object]) -> dict[str, object]:
    with _SETTINGS_LOCK:
        candidate = (
            dict(DEFAULT_SETTINGS)
            if bool(params.get("reset", False))
            else dict(_SETTINGS)
        )
        for key in DEFAULT_SETTINGS:
            if key in params:
                candidate[key] = params[key]
        count = int(candidate.get("count", 1))
        if not 0 <= count <= 999:
            raise ValueError("协力演出次数必须是0到999的整数，0表示无限")
        candidate["count"] = count
        song_choice = str(candidate.get("song_choice", "unspecified"))
        if song_choice not in COOPERATIVE_SONG_CHOICES:
            raise ValueError("协力歌曲选择必须是 unspecified/random/current 之一")
        candidate["song_choice"] = song_choice
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(_SETTINGS)


def current_cooperative_settings() -> dict[str, object]:
    with _SETTINGS_LOCK:
        return dict(_SETTINGS)


def cooperative_profile_preflight(context: Context, difficulty: str) -> str | None:
    """任务一开始就校验 Profile 与环境签名，失败返回可读原因。

    原实现把 Profile 解析放在准备页的流速门禁里，自动化已经完成整段导航
    才可能被拒。这里提前用截图分辨率、固定 DPI/帧率/画质和引擎构造签名；
    旧 Profile 中的视觉设置字段只兼容读取，不参与匹配。
    """
    store = RealtimeProfileStore(PROJECT_ROOT / "profiles")
    try:
        image = context.tasker.controller.post_screencap().wait().get()
    except Exception:
        # 控制器尚未就绪时无法构造签名，交给准备页门禁处理。
        return None
    options = store.runtime_options()
    signature = EnvironmentSignature(
        frame_resolution(image),
        COOPERATIVE_DPI,
        COOPERATIVE_GAME_FPS,
        COOPERATIVE_RENDER_QUALITY,
        1.0,
        engine=engine_from_native_flag(
            options.get("native_realtime_enabled", False)
        ),
    )
    try:
        store.resolve_latest_for_environment(
            difficulty=difficulty,
            current_signature=signature,
        )
    except ValueError as exc:
        return f"开局前 Profile 环境校验失败：{exc}"
    return None


def should_stay_in_room(settings: dict[str, object]) -> bool:
    """Only joined rooms can reuse the current room after a live."""
    return (
        str(settings.get("entry_method", "normal")) in {"friend", "private"}
        and str(settings.get("post_live_action", "exit")) == "stay"
    )


def classify_room_tier(image: np.ndarray) -> str | None:
    """Classify the selected centre room card by its stable saturated colour."""
    if (
        not isinstance(image, np.ndarray)
        or image.shape[0] < 487
        or image.shape[1] < 753
    ):
        return None
    card = image[194:487, 525:753]
    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 80)
    hues = hsv[:, :, 0][mask]
    if hues.size < 300:
        return None
    histogram = np.bincount(hues, minlength=180)
    hue = int(np.argmax(histogram))
    if 84 <= hue <= 96:
        return "free"
    if 165 <= hue <= 179:
        return "beginner"
    if 97 <= hue <= 112:
        return "chief"
    if 10 <= hue <= 28:
        return "legend"
    return None


class CooperativeLiveFlow:
    def __init__(
        self,
        context: Context,
        settings: dict[str, object],
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.context = context
        self.settings = settings
        self.effective_difficulty = str(settings["difficulty"])
        self.progress_callback = progress_callback
        self.detector = LifeDetector()
        # 准备完毕后的短黑场可能只持续一帧；漏检时先用与本局身份一致的
        # 稳定最终封面放行，只有已经进入动态演奏场才走生命监控兜底。
        self.playfield_detector = PlayfieldDetector()
        self.playfield_entry_evidence = CooperativePlayfieldEntryEvidence()
        self.song_choice_pause_pending = True
        self.templates = {
            path.stem: imread_unicode(path, cv2.IMREAD_COLOR)
            for path in TEMPLATE_DIR.glob("*.png")
        }
        missing = [name for name, image in self.templates.items() if image is None]
        if missing:
            raise RuntimeError(f"协力模板损坏：{', '.join(missing)}")

    def stopped(self) -> bool:
        return bool(self.context.tasker.stopping)

    @property
    def controller(self):
        """Always return MaaFramework's current reverse-controller proxy.

        Nested pipeline tasks may replace the proxy, so retaining the value
        seen in ``__init__`` can dereference an already released native handle.
        """
        return self.context.tasker.controller

    def capture(self) -> np.ndarray:
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        try:
            if getattr(self, "_post_score_refresh", False):
                return capture_image(self.context, node="ResultRefreshScreen")
            return capture_image(self.context)
        except ScreenRefreshCancelled as exc:
            raise InterruptedError("用户已停止任务") from exc

    def capture_startup(self) -> np.ndarray:
        """Capture short loading transitions without the normal 1.2s delays.

        Keep the callback-safe pipeline/cached-image path. Direct reverse
        post_screencap calls can hang this SDK; a dedicated node removes only
        the startup waits, preserving normal navigation timing elsewhere.
        """
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        try:
            return capture_image(self.context, node="CooperativeStartupRefreshScreen")
        except ScreenRefreshCancelled as exc:
            raise InterruptedError("用户已停止任务") from exc

    def template_box(
        self,
        image: np.ndarray,
        name: str,
        threshold: float = 0.90,
    ) -> tuple[int, int, int, int] | None:
        template = self.templates[name]
        x, y = TEMPLATE_POSITIONS[name]
        padding = 8
        left = max(0, x - padding)
        top = max(0, y - padding)
        right = min(image.shape[1], x + template.shape[1] + padding)
        bottom = min(image.shape[0], y + template.shape[0] + padding)
        search = image[top:bottom, left:right]
        if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
            return None
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        )
        if float(score) < threshold:
            return None
        return (
            left + int(location[0]),
            top + int(location[1]),
            int(template.shape[1]),
            int(template.shape[0]),
        )

    def visible(self, image: np.ndarray, name: str, threshold: float = 0.90) -> bool:
        return self.template_box(image, name, threshold) is not None

    def pipeline_box(self, image: np.ndarray, node: str):
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        result = self.context.run_recognition(node, image)
        if not result or not result.hit:
            return None
        return result.box

    def click(self, point: tuple[int, int]) -> None:
        _maa_click(self.context, point)

    def wait_for(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
        interval: float = 0.35,
        detect_member_exit: bool = True,
    ) -> tuple[str | None, np.ndarray]:
        deadline = time.monotonic() + timeout
        image = self.capture()
        while True:
            if detect_member_exit and self.visible(
                image,
                "member_exit_title",
                0.93,
            ):
                raise MemberExited("协力成员退出房间")
            for name in names:
                if name == "playfield":
                    if self.detector.detect(image).visible:
                        return name, image
                elif self.visible(image, name):
                    return name, image
            if time.monotonic() >= deadline:
                return None, image
            time.sleep(interval)
            image = self.capture()

    def dismiss_member_exit(self) -> None:
        image = self.capture()
        if self.visible(image, "member_exit_title", 0.93):
            # 当前版本弹窗：标题“错误”，正文“由于XX退出房间。将返回
            # 房间选择界面。”，底部居中“确定”按钮。
            self.click((638, 525))
            time.sleep(0.8)

    def _save_navigation_evidence(
        self, image: np.ndarray, phase: str, status: str, **details,
    ) -> None:
        """Keep recovery evidence with the song even after its video closes."""
        try:
            run = current_live_run()
            if run is None or not run.recording_path:
                return
            directory = Path(run.recording_path)
            if not directory.is_absolute():
                directory = PROJECT_ROOT / directory
            output = directory / "navigation" / (
                f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{phase}-{status}.png"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            if imwrite_unicode(output, image):
                details["screenshot"] = str(output.relative_to(directory))
            append_current_run_event(PROJECT_ROOT, phase, status, details=details)
        except Exception as exc:
            print(f"CooperativeLive navigation_evidence_failed={exc}", flush=True)

    def handle_connect_failed(
        self, image: np.ndarray, state: ConnectRetryState, *, phase: str,
    ) -> bool:
        """Handle the foreground modal before inspecting any page behind it."""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if self.template_box(image, "connect_failed_body", 0.90) is None:
            state.unclickable_since = None
            return False
        retry = self.template_box(image, "connect_failed_retry", 0.90)
        if retry is not None:
            x, y, width, height = retry
            button = image[y:y + height, x:x + width, :3]
            if float(np.median(cv2.cvtColor(button, cv2.COLOR_BGR2HSV)[:, :, 2])) < 220:
                retry = None
        if retry is None:
            now = time.monotonic()
            if state.unclickable_since is None:
                state.unclickable_since = now
                self._save_navigation_evidence(image, phase, "connect-retry-unrecognized")
            # Modal text can become recognizable before the footer finishes
            # fading in. Observe briefly without accepting the Home behind it.
            if now - state.unclickable_since < 2.0:
                return True
            raise RuntimeError("游戏连接失败，但未确认可点击的重试按钮，已停止本次导航")
        state.unclickable_since = None
        if time.monotonic() < state.next_click_at:
            return True
        if state.clicks >= state.limit:
            self._save_navigation_evidence(
                image, phase, "connect-retry-exhausted", attempts=state.clicks,
            )
            raise RuntimeError(f"游戏连接失败，重试{state.limit}次后仍未恢复，已停止本次导航")
        self._save_navigation_evidence(
            image, phase, "connect-retry", attempt=state.clicks + 1,
        )
        self.click((x + width // 2, y + height // 2))
        state.clicks += 1
        state.next_click_at = time.monotonic() + 1.0
        print(
            f"CooperativeLive phase={phase} action=connect-retry "
            f"attempt={state.clicks}/{state.limit}", flush=True,
        )
        return True

    def dismiss_connect_failed(self, attempts: int = 5) -> bool:
        """Retry only a confirmed connection dialog and verify its disappearance."""
        state = ConnectRetryState(limit=max(1, min(5, int(attempts))))
        deadline = time.monotonic() + state.limit * 2.0 + 2.0
        while time.monotonic() < deadline:
            image = self.capture()
            try:
                if not self.handle_connect_failed(image, state, phase="escape-reconnect"):
                    return True
            except RuntimeError as exc:
                print(f"CooperativeDisconnectJump connect_retry_failed={exc}", flush=True)
                return False
            time.sleep(.2)
        self._save_navigation_evidence(image, "escape-reconnect", "timeout")
        return False

    def _adb_shell(self, args) -> tuple[int, str]:
        # Shared, tested adapter supplies missing SDK signatures and reads the
        # actual process exit code; nonempty shell output is not success.
        from .team_recovery import maa_shell
        return maa_shell(lambda: self.controller, args)

    def _escape_popup_visible(self, image: np.ndarray, name: str) -> bool:
        variants = (name,)
        if name == "disconnect_confirm_body":
            variants += ("disconnect_confirm_body_current",)
        return any(
            variant in self.templates and self.visible(image, variant, 0.93)
            for variant in variants
        )

    def _wait_and_click(
        self,
        name: str,
        point: tuple[int, int],
        timeout: float,
        *,
        next_name: str | None = None,
        confirm_disappeared: bool = False,
    ) -> bool:
        """Only retry a recognized dialog; confirm the requested transition."""
        deadline = time.monotonic() + float(timeout)
        next_click = 0.0
        clicks = 0
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture()
            if next_name and self._escape_popup_visible(image, next_name):
                return True
            visible = self._escape_popup_visible(image, name)
            if clicks and confirm_disappeared and not visible:
                return True
            if visible and clicks < 3 and time.monotonic() >= next_click:
                self.click(point)
                clicks += 1
                next_click = time.monotonic() + 1.0
                if not next_name and not confirm_disappeared:
                    return True
            time.sleep(0.35)
        return False

    def disconnect_jump_out(self, *, popup_timeout_s: float = 25.0) -> bool:
        """生命归零后的断网跳车流程（AGENTS 第 22 条）。

        真实弹窗顺序（2026-09-07 雷电录像）：按游戏 UID 屏蔽出口流量 → 游戏
        退后台再切回 → 弹窗1“通信已中断。是否继续演出？”点左侧“中断” →
        弹窗2“确认中断当前演出返回主页吗？”点右侧“中断” → 恢复网络 →
        “连接失败。”弹窗有界点“重试”直到回主页。任何一步失败都在 finally
        恢复网络（fail-closed），绝不把模拟器留在断网状态。
        """
        import uuid
        gate = GameNetworkGate(self._adb_shell, chain_suffix="mbdr_coop_" + uuid.uuid4().hex[:8])
        try:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            required = {
                "disconnect_continue_body",
                "disconnect_confirm_body",
            }
            if not required.issubset(self.templates):
                # 模板未提取时不做任何网络/前台扰动，直接 fail-closed。
                print(
                    "CooperativeDisconnectJump popup_template_missing=true",
                    flush=True,
                )
                return False
            if not gate.block():
                print(
                    "CooperativeDisconnectJump gate_block_failed=true "
                    f"reason={gate.last_error or 'unknown'}",
                    flush=True,
                )
                return False
            # 退后台再切回；断网状态下切回会触发“是否切换到单人演奏”弹窗。
            self.controller.post_click_key(3).wait()
            time.sleep(0.6)
            self.controller.post_start_app(GAME_PACKAGE).wait()
            wait_for_game_capture_ready(self.context)
            if not self._wait_and_click(
                "disconnect_continue_body",
                DISCONNECT_CONTINUE_INTERRUPT_POINT,
                popup_timeout_s,
                next_name="disconnect_confirm_body",
            ):
                print(
                    "CooperativeDisconnectJump continue_popup_missed=true",
                    flush=True,
                )
                return False
            if not self._wait_and_click(
                "disconnect_confirm_body",
                DISCONNECT_CONFIRM_INTERRUPT_POINT,
                10.0,
                confirm_disappeared=True,
            ):
                print(
                    "CooperativeDisconnectJump confirm_popup_missed=true",
                    flush=True,
                )
                return False
            # 先恢复网络，再对“连接失败。”做有界重试；断网状态下点重试
            # 无效是游戏正常表现。
            if not gate.restore():
                print(
                    "CooperativeDisconnectJump network_restore_failed=true",
                    flush=True,
                )
                return False
            time.sleep(0.8)
            if not self.dismiss_connect_failed():
                print("CooperativeDisconnectJump connect_retry_exhausted=true", flush=True)
                return False
            print("CooperativeDisconnectJump completed=true", flush=True)
            return True
        finally:
            restored = False
            for _ in range(3):
                try:
                    restored = gate.restore()
                except Exception as exc:
                    print(f"CooperativeDisconnectJump restore_error={exc}", flush=True)
                if restored:
                    break
            if not restored:
                raise JumpOutUnavailable("游戏网络恢复未确认，已停止自动重试，请检查模拟器网络")

    def ensure_room_page(self, timeout: float = 15.0) -> np.ndarray:
        state, image = self.wait_for(("room_search",), timeout=timeout)
        if state is None:
            raise RuntimeError("未识别协力房间选择页")
        return image

    def close_sss_guide(self) -> None:
        # 调用前 select_normal_room 已确认传说卡居中选中；一次性 SSS
        # 引导若存在会立即出现。没有引导关闭按钮说明本账号早已关过，
        # 直接返回，不再按固定 8 帧空等（实测每局浪费约 8 秒）。
        for attempt in range(8):
            image = self.capture()
            if self.visible(image, "sss_guide_close"):
                self.click((980, 648))
                time.sleep(0.7)
                return
            if classify_room_tier(image) == "legend":
                return
            time.sleep(0.2)

    def select_normal_room(self) -> None:
        started = time.monotonic()
        image = self.ensure_room_page()
        target = str(self.settings["room_tier"])
        if target not in ROOM_TIER_INDEX:
            raise ValueError(f"不支持的协力房间档位：{target}")
        actual = classify_room_tier(image)
        print(
            "CooperativeLive select_room reached_selection "
            f"target={target} actual={actual or 'unknown'} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )

        # Never reset the carousel to Free before selecting another room.
        # Free and Legend are the two endpoints, so swipe straight toward that
        # endpoint.  If the game snaps only one card per gesture, repeat in the
        # same direction and re-read the centre card; never traverse the wrong
        # way first.  Middle tiers use the current classified index directly.
        swipes = 0
        for _ in range(4):
            if actual == target:
                break
            if target == "free":
                start, end, duration = (250, 360), (1050, 360), 500
            elif target == "legend":
                start, end, duration = (1050, 360), (250, 360), 500
            elif actual in ROOM_TIER_INDEX:
                moving_right = (
                    ROOM_TIER_INDEX[target] > ROOM_TIER_INDEX[actual]
                )
                start, end, duration = (
                    ((820, 360), (455, 360), 280)
                    if moving_right
                    else ((455, 360), (820, 360), 280)
                )
            else:
                # Unknown centre card: move toward the nearest known endpoint
                # once, then let the next classified frame choose direction.
                if ROOM_TIER_INDEX[target] <= 1:
                    start, end, duration = (250, 360), (1050, 360), 500
                else:
                    start, end, duration = (1050, 360), (250, 360), 500
            _maa_swipe(self.context, start, end, duration)
            swipes += 1
            time.sleep(0.45)
            image = self.capture()
            actual = classify_room_tier(image)

        if actual != target:
            raise RuntimeError(
                f"协力房间档位复核失败：期望 {target}，识别为 {actual or 'unknown'}"
            )
        if target == "legend":
            self.close_sss_guide()
        print(
            "CooperativeLive select_room ready_to_click "
            f"target={target} swipes={swipes} "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )
        self.click((1060, 650))
        self.verify_room_entry(
            "点击所选协力房间后仍停留在房间选择页，未开始匹配"
        )
        print(
            "CooperativeLive select_room room_entry_confirmed "
            f"elapsed={time.monotonic() - started:.2f}s",
            flush=True,
        )

    def verify_room_entry(self, failure_reason: str) -> None:
        deadline = time.monotonic() + 30.0
        departed_frames = 0
        while time.monotonic() < deadline:
            image = self.capture()
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("协力成员退出房间")
            if any(
                self.visible(image, name)
                for name in ("room_wait", "song_unspecified", "ready_button")
            ):
                print(
                    "CooperativeLive room_entry=confirmed marker=known-lobby",
                    flush=True,
                )
                return
            if self.visible(image, "room_search"):
                departed_frames = 0
            else:
                departed_frames += 1
                if departed_frames >= 3:
                    # Matchmaking/loading/member collection layouts are
                    # transient and account/network dependent.  Stable
                    # departure from the selection page is sufficient here;
                    # wait_for_preparation owns the longer progression check.
                    print(
                        "CooperativeLive room_entry=confirmed "
                        "marker=left-room-selection",
                        flush=True,
                    )
                    return
            time.sleep(0.25)
        raise RuntimeError(failure_reason)

    def open_room_search(self) -> None:
        self.ensure_room_page()
        self.click((665, 650))
        state, _ = self.wait_for(
            ("search_private", "search_friend"), timeout=8.0
        )
        if state is None:
            raise RuntimeError("点击房间搜索后未出现搜索方式弹窗")

    def enter_friend_room(self) -> None:
        self.open_room_search()
        self.click((852, 528))
        state, _ = self.wait_for(("friend_invite_title",), timeout=10.0)
        if state is None:
            raise RuntimeError("未进入好友邀请房间列表")
        self.click((1038, 237))
        self.verify_room_entry("好友邀请已失效、列表为空或未能进入房间")

    def enter_private_room(self) -> None:
        code = str(self.settings.get("room_code", "")).strip()
        if len(code) != 6 or not code.isdecimal():
            raise ValueError("房间号必须是6位数字")
        self.open_room_search()
        self.click((635, 528))
        state, _ = self.wait_for(("private_room_title",), timeout=8.0)
        if state is None:
            raise RuntimeError("未打开私人房间号输入框")
        self.click((640, 370))
        require_game_foreground(self.controller)
        self.controller.post_input_text(code).wait()
        time.sleep(0.4)
        self.click((767, 474))
        self.verify_room_entry("私人房间号无效、房间已关闭或未能进入房间")

    def check_escape_available(self) -> GameNetworkGate | None:
        if bool(self.settings.get("disconnect_jump_enabled", False)):
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            gate = GameNetworkGate(self._adb_shell)
            available = gate.check_available()
            root_failure = None
            if not available and gate.root_access_available is False:
                # MuMu can expose root through adbd without installing su.
                # Acquire it before joining, when no song inputs are armed.
                discard_prearmed_backend("cooperative-adb-root")
                print(
                    "[任务][协力演出][断网逃生][INFO] "
                    "当前 ADB 没有 root，正在自动启用并等待连接恢复", flush=True,
                )
                enabled, detail = enable_adb_root(
                    self.controller, stopping=self.stopped,
                )
                if enabled:
                    if self.stopped():
                        raise InterruptedError("用户已停止任务")
                    # adbd restart closes Maa's existing minitouch pipe.
                    # Reconnect before any navigation or Native prearming.
                    connection = self.controller.post_connection().wait()
                    if not connection.succeeded:
                        raise JumpOutUnavailable(
                            "ADB root 已启用，但 Maa 截图/触控连接恢复失败"
                        )
                    # The first gate cached a non-root shell. Verify the new
                    # permission through Maa's current controller as well.
                    gate = GameNetworkGate(self._adb_shell)
                    available = gate.check_available()
                else:
                    root_failure = detail
            if not available:
                raise JumpOutUnavailable(
                    "已开启断网逃生，但进房前检查失败，停止本次协力："
                    f"{root_failure or gate.last_error}"
                )
            print(
                "[任务][协力演出][断网逃生][INFO] "
                "进房前已确认 root、防火墙和游戏 UID 匹配可用", flush=True,
            )
            return gate
        return None

    def enter_room(self) -> None:
        self.check_escape_available()
        # The first entry can also encounter a restriction left by a prior run.
        self.navigate_to_cooperative_room_selection("entry")
        method = str(self.settings["entry_method"])
        if method == "normal":
            self.select_normal_room()
        elif method == "friend":
            self.enter_friend_room()
        elif method == "private":
            self.enter_private_room()
        else:
            raise ValueError(f"不支持的协力入房方式：{method}")

    def pause_for_song_filter(self) -> str | None:
        """留出筛歌窗口，同时观察停止、成员退出和玩家提前确认。"""
        deadline = time.monotonic() + COOPERATIVE_SONG_CHOICE_PAUSE_SECONDS
        while True:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"), timeout=0.0,
            )
            if state == "ready_button":
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 后续输入只消费最新画面；选曲页消失时由外层继续观察。
                return state
            time.sleep(min(0.1, remaining))

    def wait_for_preparation(self) -> None:
        choice_deadline = (
            time.monotonic() + ROOM_SONG_CHOICE_TIMEOUT_SECONDS
        )
        while time.monotonic() < choice_deadline:
            state, _ = self.wait_for(
                ("song_unspecified", "ready_button"),
                timeout=min(
                    3.0,
                    max(0.1, choice_deadline - time.monotonic()),
                ),
            )
            if state == "ready_button":
                # 首轮已提前确认时不把筛歌窗口顺延到后续轮次。
                self.song_choice_pause_pending = False
                return
            if state == "song_unspecified":
                song_choice = str(
                    getattr(self, "settings", {}).get("song_choice", "unspecified")
                )
                if (
                    song_choice != "unspecified"
                    and getattr(self, "song_choice_pause_pending", False)
                ):
                    self.song_choice_pause_pending = False
                    print(
                        "CooperativeLive song_choice_pause "
                        f"seconds={COOPERATIVE_SONG_CHOICE_PAUSE_SECONDS} choice={song_choice}",
                        flush=True,
                    )
                    state = self.pause_for_song_filter()
                    if state == "ready_button":
                        return
                    if state != "song_unspecified":
                        continue
                ready_deadline = (
                    time.monotonic()
                    + SONG_CHOICE_TO_READY_TIMEOUT_SECONDS
                )
                if song_choice == "random":
                    self.click(COOPERATIVE_SONG_RANDOM_POINT)
                    time.sleep(0.35)
                elif song_choice == "unspecified":
                    self.click(COOPERATIVE_SONG_UNSPECIFIED_POINT)
                    time.sleep(0.35)
                self.click(COOPERATIVE_SONG_CONFIRM_POINT)
                print(f"CooperativeLive song_choice={song_choice}", flush=True)
                time.sleep(0.5)
                while time.monotonic() < ready_deadline:
                    ready_state, _ = self.wait_for(
                        ("ready_button",),
                        timeout=min(
                            3.0,
                            max(0.1, ready_deadline - time.monotonic()),
                        ),
                    )
                    if ready_state == "ready_button":
                        return
                raise RuntimeError(
                    "确认协力选曲后60秒内未进入演出准备页"
                )
        self.jump_after_startup_failure(
            "进入协力房间后180秒内未出现不指定歌曲或准备页，"
            "已退后台返回游戏"
        )

    @staticmethod
    def action_argv(params: dict[str, object]):
        return SimpleNamespace(custom_action_param=json.dumps(params, ensure_ascii=False))

    def prepare(self) -> None:
        difficulty = str(self.settings["difficulty"])
        difficulty_params = {
            "difficulty": difficulty,
            "max_attempts": 3,
            "verify_delay_seconds": 0.25,
            "identity_read_attempts": 2,
            "identity_retry_delay_seconds": 0.15,
            "difficulty_targets": COOPERATIVE_DIFFICULTY_TARGETS,
            "song_level_roi": (130, 580, 56, 38),
            "song_title_roi": (105, 535, 290, 52),
            "song_identity": False,
            "mode": "cooperative",
            "debug_recording": bool(self.settings["debug_recording"]),
        }
        if difficulty == "Special":
            # 协力歌曲由房间决定；Special 不存在时显式回退 Expert，后续流程
            # 必须只消费实际选中的难度，不能继续拿 Special 谱面演奏。
            difficulty_params["fallback_difficulties"] = ["Expert"]
        if not RealtimeDifficultySelect().run(
            self.context, self.action_argv(difficulty_params)
        ):
            raise RuntimeError(f"协力准备页未能选择并复核 {difficulty} 难度")
        run = current_live_run()
        if run is None or not run.prepared_for_play:
            raise RuntimeError("协力难度选择成功但缺少本局实际难度证据")
        effective_difficulty = str(run.difficulty)
        if effective_difficulty != difficulty and not (
            difficulty == "Special" and effective_difficulty == "Expert"
        ):
            raise RuntimeError(
                "协力实际难度不符合回退策略："
                f"请求 {difficulty}，实际 {effective_difficulty}"
            )
        self.effective_difficulty = effective_difficulty

        performance_params = {
            "difficulty": effective_difficulty,
            "require_profile": True,
            "dpi": COOPERATIVE_DPI,
            "game_fps": COOPERATIVE_GAME_FPS,
            "render_quality": COOPERATIVE_RENDER_QUALITY,
            "coordinates": {"gear": (946, 650)},
            "defer_native_prearm": True,
            "cache_preparation_image": True,
        }
        if not RealtimePerformanceSettingsGate().run(
            self.context, self.action_argv(performance_params)
        ):
            raise RuntimeError("协力准备页流速复核失败")
        run = current_live_run()
        initial_image = None if run is None else run.cooperative_prestart_image
        ready_image = self.ensure_performance_mode_off(
            initial_image=initial_image,
        )
        if isinstance(ready_image, np.ndarray):
            # Retain the selected difficulty/level before loading replaces it.
            # The recorder is created after ready, often on a black frame.
            update_live_run(preparation_identity_image=ready_image.copy())
        ready_transition = self.ready_up_and_verify(initial_image=ready_image)
        if ready_transition != "black":
            self.watch_member_exit_before_black()
        print(
            "CooperativeLive ready=true "
            f"requested_difficulty={difficulty} "
            f"effective_difficulty={effective_difficulty} "
            "speed_gate=verified",
            flush=True,
        )

    def ensure_performance_mode_off(
        self,
        *,
        initial_image: np.ndarray | None = None,
    ) -> np.ndarray | None:
        """协力房间页关闭 3D/MV 演出表现，防止演出场背景变化提前触发谱面。

        房间页左下角与单人准备页同布局：循环箭头切换按钮位于
        ``MODE_TOGGLE_POINT``，其右侧标签显示当前模式。标签区域读不到
        强饱和色即视为 OFF。点击后仍无法确认关闭（例如界面改版或坐标
        漂移）时不阻断本局：保留证据截图并继续，让既有门控推进演出。
        """
        image = initial_image
        for attempt in range(4):
            reused = image is not None
            if image is None:
                image = self.capture()
            if live_performance_mode_is_off(image):
                print(
                    "CooperativeLive performance_mode=off confirmed=true "
                    f"reused_preparation_image={str(reused).lower()}",
                    flush=True,
                )
                return image
            if attempt == 0:
                self._save_performance_mode_evidence(image, "before")
            self.click(MODE_TOGGLE_POINT)
            time.sleep(0.6)
            image = None
        try:
            self._save_performance_mode_evidence(self.capture(), "after")
        except InterruptedError:
            raise
        print(
            "CooperativeLive performance_mode=off confirmed=false "
            "action=continue-with-warning attempts=4",
            flush=True,
        )
        return None

    def _save_performance_mode_evidence(self, image: np.ndarray, stage: str) -> None:
        try:
            evidence_dir = PROJECT_ROOT / "debug"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            path = evidence_dir / (
                "cooperative-performance-mode-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{stage}.png"
            )
            imwrite_unicode(path, image)
            print(f"CooperativeLive performance_mode_evidence={path}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 证据失败不阻断演出流程
            print(
                "CooperativeLive performance_mode_evidence_failed="
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def ready_up_and_verify(
        self,
        *,
        initial_image: np.ndarray | None = None,
    ) -> str:
        """点击“准备完毕”并确认按钮消失，防止触控未送达造成空演奏。"""
        self._ready_delivery_image = None
        image = initial_image
        for attempt in range(3):
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            reused = image is not None
            if image is None:
                image = self.capture()
            box = self.template_box(image, "ready_button", 0.90)
            if box is None and reused:
                # 缓存帧只用于加速肯定匹配；若没看到按钮，必须再采一张新图，
                # 避免拿过期画面把“按钮缺失”误判成已经准备完毕。
                image = self.capture()
                reused = False
                box = self.template_box(image, "ready_button", 0.90)
            if box is None:
                # 按钮已消失：已进入准备完毕/成员等待或加载流程。
                print(
                    f"CooperativeLive ready=confirmed attempt={attempt + 1}",
                    flush=True,
                )
                return "already-confirmed"
            left, top, width, height = box
            print(
                "CooperativeLive ready_click "
                f"attempt={attempt + 1} "
                f"reused_preparation_image={str(reused).lower()}",
                flush=True,
            )
            self.click((left + width // 2, top + height // 2))
            delivery = self.watch_ready_delivery_after_click()
            if delivery != "still-visible":
                print(
                    "CooperativeLive ready=confirmed "
                    f"attempt={attempt + 1} delivery={delivery}",
                    flush=True,
                )
                return delivery
            image = None
        raise RuntimeError("点击准备完毕后按钮仍在，触控可能未送达")

    def watch_ready_delivery_after_click(
        self,
        timeout: float = READY_DELIVERY_OBSERVE_SECONDS,
    ) -> str:
        """点击后高频观察送达，避免固定睡眠吞掉短黑场转场。"""
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            image = self.capture_startup()
            if self.visible(image, "member_exit_title", 0.93):
                self.dismiss_member_exit()
                raise MemberExited("协力成员退出房间")
            if _frame_is_black_transition(image):
                outcome = "black"
            elif self.template_box(image, "ready_button", 0.90) is None:
                outcome = "button-gone"
            else:
                time.sleep(0.05)
                continue
            # The delivery frame can already be the final jacket page. Hand
            # it to the transition resolver instead of discarding this chance.
            self._ready_delivery_image = image
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            print(
                "CooperativeLive ready_delivery "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome
        print(
            "CooperativeLive ready_delivery outcome=still-visible "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
            f"timeout_s={float(timeout):.3f}",
            flush=True,
        )
        return "still-visible"

    def make_final_cover_entry_resolver(self) -> FinalCoverResolver | None:
        """构造准备后封面转场识别器，只接受与本局身份一致的稳定封面。"""
        run = current_live_run()
        if run is None:
            return None
        return FinalCoverResolver(
            difficulty=run.difficulty,
            observed_level=run.song_level,
            observed_title=run.song_title,
            observed_title_confidence=float(run.song_title_confidence or 0.0),
            repository=LocalChartRepository(
                PROJECT_ROOT / "resource" / "charts"
            ),
            reject_member_loading=cooperative_member_loading_guard_enabled(),
        )

    def watch_member_exit_before_black(
        self,
        timeout: float = MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> str:
        """准备完毕到黑场转场之间的成员退出弹窗窗口。

        点击“准备完毕”后、进入演奏的整屏黑场之前，其他成员退出时仍会弹出
        “错误/由于XX退出房间。”；此时已离开房间等待页，常规 wait_for 的
        成员退出检测不再覆盖，弹窗会挡住转场导致整局卡死。这里高频轮询到
        黑场出现为止：看到弹窗就点“确定”并按成员退出策略处理；看到黑场
        说明转场已开始，弹窗不再可能，立即退出本窗口。若短黑场漏检，连续
        两帧稳定且能由本局难度、等级、标题共同确认的最终封面也可证明正常
        转场，并把该证据直接交给演奏入口；只有已经进入动态演奏场时才进入
        只监控生命的 fail-closed 路径，绝不能把谱面开头锚到中段。静态准备页
        可能误中生命条与判定线，60 秒超时同样安全跳车并停止任务。
        """
        # 每局准备后窗口都从空白动态基线开始，绝不能让上一局未完成的局部
        # 变化跨局累积成“已错过转场”的第二次证据。
        self.playfield_entry_evidence.reset()
        final_cover_resolver = self.make_final_cover_entry_resolver()
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        initial_image = getattr(self, "_ready_delivery_image", None)
        self._ready_delivery_image = None
        capture_count = 0
        capture_max_ms = 0.0
        recent_frames = deque(maxlen=8)

        def finish(outcome: str) -> str:
            elapsed_ms = (time.monotonic() - started_at) * 1000.0
            print(
                "CooperativeLive ready_transition "
                f"outcome={outcome} elapsed_ms={elapsed_ms:.1f} "
                f"capture_node=CooperativeStartupRefreshScreen "
                f"capture_count={capture_count} capture_max_ms={capture_max_ms:.1f} "
                f"timeout_s={float(timeout):.3f}",
                flush=True,
            )
            return outcome

        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            capture_started = time.monotonic()
            if initial_image is not None:
                image, initial_image = initial_image, None
            else:
                image = self.capture_startup()
                capture_count += 1
                capture_max_ms = max(
                    capture_max_ms, (time.monotonic() - capture_started) * 1000.0,
                )
            recent_frames.append((time.monotonic() - started_at, image))
            if _frame_is_black_transition(image):
                # 整屏黑场转场已经开始，成员退出弹窗窗口已过。
                return finish("black")
            if self.visible(image, "member_exit_title", 0.93):
                finish("member-exit")
                self.dismiss_member_exit()
                raise MemberExited("协力成员退出房间")
            if final_cover_resolver is not None:
                cover_resolution = final_cover_resolver.observe(image)
                if cover_resolution is not None:
                    update_live_run(
                        startup_final_cover_image=image.copy(),
                        startup_final_cover_resolution=cover_resolution,
                    )
                    return finish("final-cover")
            playfield_visible = self.playfield_detector(image)
            if self.playfield_entry_evidence.observe(
                image,
                playfield_visible=playfield_visible,
            ):
                finish("playfield-motion-missed-transition")
                self._save_startup_failure_evidence(
                    recent_frames, "playfield-motion-missed-transition",
                )
                self.wait_for_life_depleted_after_missed_transition(image)
            time.sleep(0.02)
        finish("timeout")
        self._save_startup_failure_evidence(recent_frames, "transition-timeout")
        self.jump_after_download_timeout()

    def _save_startup_failure_evidence(self, frames, status: str) -> None:
        """Save a bounded preflight sequence even before Play creates its recorder."""
        if self.stopped():
            return
        try:
            run = current_live_run()
            if run is None or not self.settings.get("diagnostic_trace", True):
                return
            directory = PROJECT_ROOT / "debug" / "cooperative-startup" / run.run_id
            directory.mkdir(parents=True, exist_ok=True)
            entries = []
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            for index, (elapsed, image) in enumerate(frames):
                if self.stopped():
                    return
                path = directory / f"{stamp}-{index:02d}-{status}.png"
                if imwrite_unicode(path, image):
                    entries.append({"elapsed_seconds": elapsed, "screenshot": path.name})
            preparation = getattr(run, "preparation_identity_image", None)
            if isinstance(preparation, np.ndarray) and not self.stopped():
                path = directory / f"{stamp}-preparation.png"
                if imwrite_unicode(path, preparation):
                    entries.append({"phase": "preparation-identity", "screenshot": path.name})
            payload = {
                "run_id": run.run_id, "status": status,
                "song_title": run.song_title, "song_level": run.song_level,
                "difficulty": run.difficulty, "frames": entries,
            }
            (directory / f"{stamp}-evidence.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            print(f"CooperativeLive startup_evidence={directory} status={status}", flush=True)
        except Exception as exc:
            print(f"CooperativeLive startup_evidence_failed={exc}", flush=True)

    def wait_for_life_depleted_after_missed_transition(
        self,
        initial_image: np.ndarray,
        *,
        timeout: float = MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        """错过黑场后只监控生命归零，绝不在歌曲中段启动演奏引擎。"""
        guard = LifeGuard(confirm_frames=3)
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        image = initial_image
        visible_samples = 0
        invisible_samples = 0

        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            reading = self.detector.detect(image)
            if reading.visible:
                visible_samples += 1
            else:
                invisible_samples += 1
            status = guard.update(reading)
            if status is LifeStatus.DEAD:
                print(
                    "CooperativeLive missed_transition_life_monitor "
                    "outcome=life-depleted "
                    f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
                    f"minimum_value={guard.minimum} "
                    f"visible_samples={visible_samples} "
                    f"invisible_samples={invisible_samples}",
                    flush=True,
                )
                self.jump_after_startup_failure(
                    "准备完毕后错过开演转场，已确认生命归零并退后台返回游戏"
                )
            time.sleep(0.2)
            image = self.capture()

        print(
            "CooperativeLive missed_transition_life_monitor "
            "outcome=timeout "
            f"elapsed_ms={(time.monotonic() - started_at) * 1000.0:.1f} "
            f"minimum_value={guard.minimum} "
            f"alive_confirmed={guard.alive_confirmed} "
            f"visible_samples={visible_samples} "
            f"invisible_samples={invisible_samples}",
            flush=True,
        )
        self.jump_after_startup_failure(
            "准备完毕后错过开演转场，生命监控超时，已退后台返回游戏"
        )

    def jump_after_startup_failure(self, reason: str) -> None:
        """Exit invalid startup without starting mid-song; recover if enabled."""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if bool(self.settings.get("disconnect_jump_enabled", False)):
            self.recover_failed_live_exit()
            print(
                "CooperativeLive startup_recovered=true retry_pending=true "
                f"reason={reason}", flush=True,
            )
            raise CooperativeStartupRetry(reason)
        require_game_foreground(self.controller)
        self.controller.post_click_key(3).wait()
        time.sleep(0.6)
        self.controller.post_start_app(GAME_PACKAGE).wait()
        wait_for_game_capture_ready(self.context)
        deadline = time.monotonic() + 12.0
        foreground_confirmed = False
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            if foreground_package(self.controller) == GAME_PACKAGE:
                foreground_confirmed = True
                break
            time.sleep(0.5)
        record_failure_reason(reason)
        print(
            "CooperativeLive startup_jump "
            f"foreground_confirmed={str(foreground_confirmed).lower()} "
            f"reason={reason}",
            flush=True,
        )
        raise JumpOutUnavailable(reason)

    def jump_after_download_timeout(self) -> None:
        reason = "成员下载超过60秒仍未进入演出，已主动跳车并返回游戏"
        self.jump_after_startup_failure(reason)

    def wait_for_playfield(self) -> None:
        state, _ = self.wait_for(
            ("playfield",),
            timeout=MEMBER_DOWNLOAD_TIMEOUT_SECONDS,
            interval=0.2,
        )
        if state != "playfield":
            self.jump_after_download_timeout()
        print("CooperativeLive member_download=complete playfield_visible=true", flush=True)

    def play(self) -> bool:
        params = cooperative_play_params(
            self.settings,
            effective_difficulty=getattr(
                self,
                "effective_difficulty",
                str(self.settings.get("difficulty", "Expert")),
            ),
        )
        success = RealtimeProfilePlay().run(
            self.context, self.action_argv(params)
        )
        run = current_live_run()
        jump_requested = run is not None and bool(run.disconnect_jump_requested)
        if jump_requested or not success:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            if bool(self.settings.get("disconnect_jump_enabled", False)):
                self.recover_failed_live_exit()
                return False  # outer retry loop verifies home and re-enters
            if jump_requested:
                raise JumpOutUnavailable("生命归零，未启用自动断网跳车，请手动退出后重试")
            return False
        return True

    def recover_failed_live_exit(self) -> None:
        """Exit a failed round after Native cleanup; never count it completed."""
        discard_prearmed_backend("cooperative-failed-live-exit")
        try:
            exited = self.disconnect_jump_out()
        except (InterruptedError, JumpOutUnavailable):
            raise
        except Exception as exc:
            # disconnect_jump_out always confirms network cleanup in finally.
            print(f"CooperativeDisconnectJump fallback_restart={type(exc).__name__}: {exc}", flush=True)
            exited = False
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if not exited:
            # Root or popup unavailable: restart only the game, not emulator/ADB.
            stopped = self.controller.post_stop_app(GAME_PACKAGE).wait()
            if not stopped.succeeded:
                raise JumpOutUnavailable("协力失败后无法确认游戏已关闭，停止自动重试")
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            started = self.controller.post_start_app(GAME_PACKAGE).wait()
            if not started.succeeded:
                raise JumpOutUnavailable("协力失败后游戏重启失败，停止自动重试")
            wait_for_game_capture_ready(self.context)
            time.sleep(2.0)
        print("CooperativeLive failed_round_exited=true retry_pending=true", flush=True)

    def wait_for_post_score_destination(
        self,
        names: tuple[str, ...],
        *,
        timeout: float,
        connection_retry: ConnectRetryState | None = None,
    ) -> str | None:
        """Recognise a result exit before any post-Back corner tap.

        Cooperative result pages do not have a fixed count.  ``room_search``
        and ``live_entry`` are local templates; the home marker reuses the
        same pipeline recogniser as task startup so its proven 0.82 threshold
        remains the single source of truth.
        """
        # 演出结束后的结算页面不会再出现“成员退出”弹窗；此处关闭该检查，
        # 避免结算导航被残留模板命中打断，把弹窗处理限制在房间/准备阶段。
        # 一帧同时检查出口和剧情，不为尚未到达的房间页空等两轮超时。
        self._post_score_refresh = True
        try:
            image = self.capture()
        finally:
            self._post_score_refresh = False
        if self.handle_connect_failed(
            image, connection_retry or ConnectRetryState(), phase="post-score",
        ):
            time.sleep(.2)
            return "story"
        for name in names:
            if self.visible(image, name):
                self._quit_cancel_home_pending = False
                return name
        if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
            self._quit_cancel_home_pending = False
            return "home"
        # 该确认框提供主页到达证据；取消后复核弹窗消失，再按主页终点收尾。
        # 若仍返回剧情状态且主页模板漏识别，外层可能再次按 BACK 打开弹窗。
        quit_box = self.pipeline_box(image, "QuitConfirmCancel")
        if quit_box is None and getattr(self, "_quit_cancel_home_pending", False):
            # 取消动画可能跨越多帧；弹窗消失后消费之前的主页证据，期间不发 BACK。
            self._quit_cancel_home_pending = False
            return "home"
        if quit_box is not None:
            self.click(
                (
                    int(quit_box.x + quit_box.w // 2),
                    int(quit_box.y + quit_box.h // 2),
                )
            )
            self._quit_cancel_home_pending = True
            self._post_score_refresh = True
            try:
                after_cancel = self.capture()
            finally:
                self._post_score_refresh = False
            if self.pipeline_box(after_cancel, "QuitConfirmCancel") is not None:
                return "story"
            self._quit_cancel_home_pending = False
            return "home"
        if handle_story_page(
            image, recognise=self.pipeline_box, click=self.click,
            stopping=self.stopped,
        ):
            return "story"
        return None

    def advance_post_score_once(
        self,
        names: tuple[str, ...],
        *,
        inspect_timeout: float,
        connection_retry: ConnectRetryState | None = None,
    ) -> str | None:
        """完整执行安全像素→BACK→安全像素后再识别终点。"""
        def before_input() -> None:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            require_game_foreground(self.controller)

        accelerated_back(
            lambda: self.controller,
            before_input=before_input,
            phase="post-score",
            log_prefix="CooperativeResult",
        )
        return self.wait_for_post_score_destination(
            names,
            timeout=inspect_timeout,
            connection_retry=connection_retry,
        )

    def result_buttons(self, image: np.ndarray) -> dict | None:
        """Confirm both final result controls before choosing either one."""
        replay = self.template_box(image, "result_replay", 0.93)
        confirm = self.template_box(image, "result_confirm", 0.93)
        if replay is None or confirm is None:
            return None
        # Normalized template correlation also matches a dimmed page beneath
        # a modal. Require the actual bright controls, not their shaded copies.
        for box in (replay, confirm):
            x, y, width, height = box
            button = image[y:y + height, x:x + width, :3]
            if float(np.median(cv2.cvtColor(button, cv2.COLOR_BGR2HSV)[:, :, 2])) < 220:
                return None
        return {"replay": replay, "confirm": confirm}

    def navigate_completed_result(self, *, replay: bool) -> bool:
        """Choose replay between rounds, confirm on the last, then verify exit.

        Return True only when a joined room is explicitly retained. Ordinary
        replay returns to room selection and still requires normal entry checks.
        Inspect between individual cadence inputs so BACK cannot consume the
        final result page before the requested button is chosen.
        """
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        action = "replay" if replay else "confirm"
        clicks = 0
        connection_retry = ConnectRetryState()
        next_click_at = 0.0
        back_next = False
        previous_refresh = getattr(self, "_post_score_refresh", False)
        self._post_score_refresh = True
        try:
            while time.monotonic() < deadline:
                if self.stopped():
                    raise InterruptedError("用户已停止任务")
                image = self.capture()
                if self.stopped():
                    raise InterruptedError("用户已停止任务")
                if self.handle_connect_failed(image, connection_retry, phase="result"):
                    time.sleep(.2)
                    continue
                buttons = self.result_buttons(image)
                if buttons is not None:
                    if clicks >= 3 and time.monotonic() >= next_click_at:
                        raise RuntimeError("协力结算按钮点击3次后仍未消失")
                    if time.monotonic() >= next_click_at:
                        x, y, width, height = buttons[action]
                        self.click((x + width // 2, y + height // 2))
                        clicks += 1
                        next_click_at = time.monotonic() + 1.0
                        print(
                            f"CooperativeResult action={action} "
                            f"last_round={str(not replay).lower()} attempt={clicks}",
                            flush=True,
                        )
                    time.sleep(.2)
                    continue

                if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
                    if replay:
                        self.navigate_to_cooperative_room_selection("home")
                    return False
                if replay and self.visible(image, "room_search"):
                    print("CooperativeResult replay_destination=room-selection confirmed=true", flush=True)
                    return False
                if replay and self.visible(image, "live_entry"):
                    self.navigate_to_cooperative_room_selection("live_entry")
                    return False
                if self.visible(image, "repeat_room_title"):
                    if replay and should_stay_in_room(self.settings):
                        self.stay_in_room()
                        return True
                    # A joined-room prompt can follow result confirmation.
                    # Choose "no" when leaving, including the final round.
                    self.click((512, 447))
                    time.sleep(.5)
                    continue

                quit_box = self.pipeline_box(image, "QuitConfirmCancel")
                if quit_box is not None:
                    self.click((int(quit_box.x + quit_box.w // 2),
                                int(quit_box.y + quit_box.h // 2)))
                    time.sleep(.2)
                    continue
                if handle_story_page(
                    image, recognise=self.pipeline_box, click=self.click,
                    stopping=self.stopped,
                ):
                    time.sleep(.2)
                    continue

                if not clicks:
                    def before_input() -> None:
                        if self.stopped():
                            raise InterruptedError("用户已停止任务")
                        require_game_foreground(self.controller)
                    back_next = advance_result_cadence(
                        lambda: self.controller, back_next=back_next,
                        before_input=before_input, phase="post-score",
                        log_prefix="CooperativeResult",
                    )
                # Once chosen, only inspect the resulting loading/room page;
                # another BACK could leave the room selector or cancel reentry.
                time.sleep(.35)
            raise RuntimeError(
                f"协力结算60秒内未完成{'再次演出并回到房间选择' if replay else '确定并返回主页'}"
            )
        finally:
            self._post_score_refresh = previous_refresh

    def wait_for_cooperative_restriction(
        self, image: np.ndarray, *, remaining_budget: float,
    ) -> bool:
        """Acknowledge only the known restriction; respect its cooldown."""
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        if self.template_box(image, "restriction_body") is None:
            return False
        ok_box = self.template_box(image, "restriction_ok")
        if ok_box is None:
            return False
        result = self.context.run_recognition("CooperativeRestrictionCountdown", image)
        text = (
            str(getattr(getattr(result, "best_result", None), "text", ""))
            if result and result.hit else ""
        )
        # Require the full countdown, so a partial OCR result cannot turn
        # "1分钟24秒" into a 24-second wait. Unknown text gets a bounded recheck.
        match = re.fullmatch(
            r"功能限制剩余时间[:：]?(?:(\d+)小时)?(?:(\d+)分(?:钟)?)?(?:(\d+)秒)?",
            re.sub(r"\s+", "", text),
        )
        seconds = None
        if match and any(value is not None for value in match.groups()):
            hours, minutes, secs = (int(value or 0) for value in match.groups())
            seconds = hours * 3600 + minutes * 60 + secs
        wait_seconds = float(seconds + 2 if seconds is not None else 30)
        if wait_seconds > remaining_budget:
            raise RuntimeError(
                "游戏协力功能暂时受限，超过本次自动等待上限（5分钟）；"
                f"请等待游戏限制结束后再试。识别倒计时：{text or '未识别'}"
            )
        print(
            "[任务][协力演出][限制等待][INFO] "
            f"游戏协力功能暂时受限，等待{wait_seconds:g}秒后重试；"
            f"倒计时：{text or '未识别，稍后复查'}", flush=True,
        )
        x, y, width, height = ok_box
        if self.stopped():
            raise InterruptedError("用户已停止任务")
        self.click((x + width // 2, y + height // 2))
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError("用户已停止任务")
            time.sleep(max(0.0, min(.5, deadline - time.monotonic())))
        return True

    def navigate_to_cooperative_room_selection(self, origin: str) -> None:
        """Explicitly recover Home/live-select into cooperative room select."""
        deadline = time.monotonic() + 30.0
        restriction_budget = 300.0
        next_home_click_at = 0.0
        next_entry_click_at = 0.0
        connection_retry = ConnectRetryState()
        while time.monotonic() < deadline:
            image = self.capture()
            if self.handle_connect_failed(image, connection_retry, phase="room-reentry"):
                time.sleep(.2)
                continue
            wait_started = time.monotonic()
            if self.wait_for_cooperative_restriction(
                image, remaining_budget=restriction_budget,
            ):
                elapsed = time.monotonic() - wait_started
                restriction_budget -= elapsed
                # Cooldown time does not consume the ordinary navigation
                # budget, but repeated restriction dialogs remain bounded.
                deadline += elapsed
                continue
            if self.visible(image, "room_search"):
                print(
                    "CooperativeLive state=room-selection "
                    f"reentry_from={origin} confirmed=true",
                    flush=True,
                )
                return

            quit_box = self.pipeline_box(image, "QuitConfirmCancel")
            if quit_box is not None:
                # 弹窗会挡住主页“演出”按钮；点取消后再重试导航。
                self.click(
                    (
                        int(quit_box.x + quit_box.w // 2),
                        int(quit_box.y + quit_box.h // 2),
                    )
                )
                time.sleep(0.5)
                continue

            close_box = self.pipeline_box(image, "CooperativeNavigationClose")
            if close_box is not None:
                self.click(
                    (
                        int(close_box.x + close_box.w // 2),
                        int(close_box.y + close_box.h // 2),
                    )
                )
                time.sleep(0.5)
                continue

            if self.pipeline_box(image, "CooperativeHomeMarker") is not None:
                now = time.monotonic()
                if now >= next_home_click_at:
                    self.click(HOME_LIVE_POINT)
                    next_home_click_at = now + 2.0
                    print(
                        "CooperativeLive state=home action=open-live-for-next-round",
                        flush=True,
                    )
                time.sleep(0.35)
                continue

            entry_box = self.template_box(image, "live_entry")
            if entry_box is not None:
                now = time.monotonic()
                if now >= next_entry_click_at:
                    x, y, width, height = entry_box
                    self.click((x + width // 2, y + height // 2))
                    next_entry_click_at = now + 2.0
                    print(
                        "CooperativeLive state=live-select "
                        "action=open-cooperative-for-next-round",
                        flush=True,
                    )
                time.sleep(0.35)
                continue

            time.sleep(0.35)
        self._save_navigation_evidence(image, "room-reentry", "timeout", origin=origin)
        raise RuntimeError(f"从{origin}恢复协力入口时，30秒内未能进入房间选择页")

    def return_to_room_selection(self) -> None:
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        attempts = 0
        connection_retry = ConnectRetryState()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("协力结算推进60秒后仍未返回房间选择页面")
            state = self.wait_for_post_score_destination(
                ("room_search", "live_entry"),
                timeout=min(2.0, remaining),
                connection_retry=connection_retry,
            )
            if state == "story":
                continue
            if state == "room_search":
                print(
                    "CooperativeLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_cooperative_room_selection(state)
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("协力结算推进60秒后仍未返回房间选择页面")
            state = self.advance_post_score_once(
                ("room_search", "live_entry"),
                inspect_timeout=min(2.0, remaining),
                connection_retry=connection_retry,
            )
            attempts += 1
            print(
                "CooperativeLive state=post-score "
                "action=corner-back-corner-recognise"
                f" attempt={attempts}",
                flush=True,
            )
            if state == "room_search":
                print(
                    "CooperativeLive state=room-selection "
                    "result_navigation=complete",
                    flush=True,
                )
                return
            if state in {"home", "live_entry"}:
                self.navigate_to_cooperative_room_selection(state)
                return

    def stay_in_room(self) -> None:
        method = str(self.settings.get("entry_method", "normal"))
        if method not in {"friend", "private"}:
            raise ValueError("留在房间仅适用于好友邀请房间或房间号入房")
        deadline = time.monotonic() + POST_SCORE_NAVIGATION_TIMEOUT_SECONDS
        result_back_attempts = 0
        connection_retry = ConnectRetryState()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "协力结算推进60秒后仍未出现是否留在同一房间的提示"
                )
            state = self.wait_for_post_score_destination(
                ("repeat_room_title", "room_search", "live_entry"),
                timeout=min(2.0, remaining),
                connection_retry=connection_retry,
            )
            if state == "story":
                continue
            if state == "repeat_room_title":
                break
            if state in {"home", "room_search", "live_entry"}:
                raise RuntimeError(
                    "未出现是否留在同一房间的提示，当前房间已经结束"
                )
            # 结算页数量不固定。每次先完整执行三步节拍，再检查最终房间弹窗；
            # 未到终点时继续下一轮，不识别任何中间结算页面。
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "协力结算推进60秒后仍未出现是否留在同一房间的提示"
                )
            state = self.advance_post_score_once(
                ("repeat_room_title", "room_search", "live_entry"),
                inspect_timeout=min(2.0, remaining),
                connection_retry=connection_retry,
            )
            result_back_attempts += 1
            print(
                "CooperativeLive state=post-score "
                "action=corner-back-corner-recognise"
                f" attempt={result_back_attempts}",
                flush=True,
            )
            if state == "repeat_room_title":
                break
            if state in {"home", "room_search", "live_entry"}:
                raise RuntimeError(
                    "未出现是否留在同一房间的提示，当前房间已经结束"
                )
        # The exact repeat-room popup is visually identified above.  Its pink
        # “是” button is centred at (768, 447) on the canonical 1280x720 UI.
        self.click((768, 447))
        time.sleep(0.8)
        state, _ = self.wait_for(
            ("room_wait", "song_unspecified", "ready_button"),
            timeout=15.0,
        )
        if state is None:
            raise RuntimeError("已选择留在房间，但未返回协力房间等候界面")
        print("CooperativeLive repeat_room=stay confirmed=true", flush=True)

    def run_attempt(self, reuse_room: bool = False) -> bool:
        try:
            if not reuse_room:
                self.enter_room()
            self.wait_for_preparation()
            self.prepare()
            return self.play()
        except (InterruptedError, MemberExited, JumpOutUnavailable, CooperativeStartupRetry):
            raise
        except Exception as exc:
            if isinstance(exc, OSError) and "access violation" in str(exc).lower():
                # 二次访问反向控制器会用新的访问冲突掩盖最初的句柄失效。
                raise
            # 弹窗可能在任意准备步骤之间出现，统一转换为成员退出策略处理。
            try:
                image = self.capture()
            except Exception:
                raise exc
            if self.visible(image, "member_exit_title", 0.93):
                raise MemberExited("协力成员退出房间") from exc
            raise

    def recover_after_play_failure(self, reason: str) -> None:
        """完整清理失败单局并从主页重新进入协力，禁止在旧会话中续跑。"""
        discard_prearmed_backend("cooperative-play-retry")
        recovery_params = {
            "home_node": "CooperativeHomeMarker",
            "modal_cancel_nodes": ["QuitConfirmCancel"],
            "modal_retry_nodes": ["CooperativeConnectFailedRetry"],
            "modal_retry_presence_nodes": ["CooperativeConnectFailedBody"],
            "modal_retry_limit": 5,
            "modal_retry_interval_ms": 1000,
            "modal_retry_min_brightness": 220,
            "modal_retry_evidence_enabled": True,
            "click_nodes": [
                "AutoLiveLoginTap",
                "AutoLiveLoginNext",
                "AutoLiveCommonClose",
                "AutoLiveStorySkipConfirmLarge",
                "AutoLiveStorySkipConfirm",
                "AutoLiveStorySkip",
                "AutoLiveStoryMenu",
            ],
            "escape_interval_ms": 1500,
            "escape_timeout_ms": 60000,
            "restart_limit": 2,
            "restart_wait_ms": 5000,
            "startup_grace_ms": 12000,
            "login_start_node": "AutoLiveLoginScreenMarker",
            "login_start_target": [640, 635],
            "login_marker_priority_attempts": 3,
            "escape_after_login_start": True,
            "package": GAME_PACKAGE,
        }
        argv = SimpleNamespace(
            custom_action_param=json.dumps(
                recovery_params,
                ensure_ascii=False,
            )
        )
        record_failure_reason("")
        if not CommonRecover().run(self.context, argv):
            raise RuntimeError(
                "协力单局失败后无法恢复主页："
                f"{latest_failure_reason() or reason}"
            )
        self.navigate_to_cooperative_room_selection("home")

    def handle_member_exit(self, reconnects: int) -> int | None:
        policy = str(self.settings["member_exit_policy"])
        reconnect_limit = max(0, min(5, int(self.settings["max_reconnects"])))
        self.dismiss_member_exit()
        if policy != "reconnect":
            reason = "检测到协力成员退出房间，已确认弹窗并结束任务"
            record_failure_reason(reason)
            print(f"[任务][协力演出][成员退出][ERROR] {reason}", flush=True)
            return None
        if reconnects >= reconnect_limit:
            reason = f"协力成员退出后已重连{reconnect_limit}次，达到上限"
            record_failure_reason(reason)
            print(f"[任务][协力演出][重连][ERROR] {reason}", flush=True)
            return None
        reconnects += 1
        print(
            "CooperativeLive member_exit=reconnect "
            f"attempt={reconnects}/{reconnect_limit}",
            flush=True,
        )
        # 成员退出弹窗关闭后，游戏往往还停留在结算页，不能直接要求
        # “房间选择页”出现。先把剩余结算页推进回房间/主页；仍失败则走
        # 主页恢复再重进，避免因为识别不到房间页把整个任务报错停掉。
        try:
            self.return_to_room_selection()
        except MemberExited:
            try:
                self.recover_after_play_failure("成员退出弹窗反复出现")
            except Exception as recovery_error:
                raise RuntimeError(
                    "成员退出后恢复失败："
                    f"{type(recovery_error).__name__}: {recovery_error}"
                ) from recovery_error
        except InterruptedError:
            raise
        except Exception as exc:
            try:
                self.recover_after_play_failure(
                    f"成员退出后未回到房间：{type(exc).__name__}: {exc}"
                )
            except Exception as recovery_error:
                raise RuntimeError(
                    "成员退出后恢复失败："
                    f"{type(recovery_error).__name__}: {recovery_error}"
                ) from recovery_error
        return reconnects

    def run(self) -> bool:
        total = int(self.settings.get("count", 1))
        completed = 0
        reconnects = 0
        reuse_room = False
        play_failures = 0
        retry_count = max(
            0,
            min(99, int(self.settings.get(
                "play_failure_retry_count",
                3 if self.settings.get("disconnect_jump_enabled", False) else 0,
            ))),
        )
        def recover_completed_round(reason):
            try:
                self.recover_after_play_failure(reason)
            except InterruptedError:
                raise
            except Exception as exc:
                print(f"CooperativeLive post_result_warning={type(exc).__name__}: {exc}", flush=True)

        while total == 0 or completed < total:
            if self.context.tasker.stopping:
                return True
            print(
                f"CooperativeLive round={completed + 1}/{total or '无限'} "
                f"reuse_room={str(reuse_room).lower()}",
                flush=True,
            )
            try:
                success = self.run_attempt(reuse_room=reuse_room)
            except MemberExited:
                next_reconnects = self.handle_member_exit(reconnects)
                if next_reconnects is None:
                    return False
                reconnects = next_reconnects
                reuse_room = False
                continue
            except JumpOutUnavailable as exc:
                # 已执行安全跳车，或现有自动化无法继续处理时直接结束任务。
                record_failure_reason(str(exc))
                print(
                    f"[任务][协力演出][流程][ERROR] {exc}",
                    flush=True,
                )
                return False
            except InterruptedError:
                raise
            except Exception as exc:
                if play_failures >= retry_count:
                    raise
                play_failures += 1
                reason = f"{type(exc).__name__}: {exc}"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "cooperative",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "CooperativeLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "CooperativeLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue
            if self.context.tasker.stopping:
                return True
            if not success:
                if play_failures >= retry_count:
                    return False
                play_failures += 1
                reason = "RealtimeProfilePlay 返回失败"
                try:
                    append_current_run_event(
                        PROJECT_ROOT,
                        "retry",
                        "scheduled",
                        details={
                            "mode": "cooperative",
                            "attempt": play_failures + 1,
                            "attempt_limit": retry_count + 1,
                            "reason": reason,
                        },
                    )
                except Exception as evidence_error:
                    print(
                        "CooperativeLive retry_evidence_failed="
                        f"{type(evidence_error).__name__}: {evidence_error}",
                        flush=True,
                    )
                print(
                    "CooperativeLive play_retry=true "
                    f"attempt={play_failures + 1}/{retry_count + 1} "
                    f"reason={reason}",
                    flush=True,
                )
                self.recover_after_play_failure(reason)
                reuse_room = False
                continue

            completed += 1
            play_failures = 0
            callback = getattr(self, "progress_callback", None)
            if callback is not None:
                try:
                    callback(completed, total)
                except Exception as exc:
                    print(f"CooperativeLive progress_warning={type(exc).__name__}: {exc}", flush=True)
            if self.context.tasker.stopping:
                return True
            is_last = total > 0 and completed >= total

            try:
                reuse_room = self.navigate_completed_result(replay=not is_last)
            except InterruptedError:
                raise
            except Exception as exc:
                # The performance is already counted. Recover navigation for
                # the next round; final-home recovery belongs to Finalize.
                print(
                    "CooperativeLive post_score=recover "
                    f"last_round={str(is_last).lower()} "
                    f"reason={type(exc).__name__}: {exc}",
                    flush=True,
                )
                if not is_last:
                    recover_completed_round(
                        f"结算后未返回房间：{type(exc).__name__}: {exc}"
                    )
                reuse_room = False
        return True


@AgentServer.custom_action("CooperativeLiveConfigure")
class CooperativeLiveConfigure(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if context.tasker.stopping:
            return True
        try:
            settings = configure_cooperative_settings(
                json.loads(argv.custom_action_param or "{}")
            )
            print(f"CooperativeLive configured={settings}", flush=True)
            return True
        except Exception as exc:
            record_failure_reason(f"协力演出选项无效：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            return False


@AgentServer.custom_action("CooperativeLiveRecover")
class CooperativeLiveRecover(CustomAction):
    """Restore any interrupted escape before game login or room navigation."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            settings = current_cooperative_settings()
            progress = SimpleNamespace(
                custom_action_param=json.dumps({
                    "task_name": "CooperativeLive", "label": "协力演出",
                    "total": int(settings["count"]), "phase": "initialize",
                }),
                task_detail=getattr(argv, "task_detail", None),
            )
            if not TaskProgress().run(context, progress):
                raise RuntimeError("协力演出次数初始化失败")
            flow = CooperativeLiveFlow(context, settings)
            gate = flow.check_escape_available()
            if gate is not None and not gate.restore_stale_escapes():
                raise JumpOutUnavailable("上次中断的游戏网络未恢复：" + str(gate.last_error))
            return CommonRecover().run(context, argv)
        except InterruptedError:
            return bool(context.tasker.stopping)
        except Exception as exc:
            reason = f"协力启动恢复失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            print(f"[任务][协力演出][启动][ERROR] {reason}", flush=True)
            return False


@AgentServer.custom_action("CooperativeLiveFlow")
class CooperativeLiveAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            settings = current_cooperative_settings()
            settings["play_failure_retry_count"] = int(
                RealtimeProfileStore(
                    PROJECT_ROOT / "profiles"
                ).runtime_options().get("play_failure_retry_count", 1)
            )
            preflight_error = cooperative_profile_preflight(
                context, str(settings["difficulty"])
            )
            if preflight_error is not None:
                record_failure_reason(preflight_error)
                print(
                    f"[任务][协力演出][流程][ERROR] {preflight_error}",
                    flush=True,
                )
                return False

            def progress_argv(phase: str, total: int):
                return SimpleNamespace(
                    custom_action_param=json.dumps(
                        {
                            "task_name": "CooperativeLive",
                            "label": "协力演出",
                            "total": total,
                            "phase": phase,
                        },
                        ensure_ascii=False,
                    ),
                    task_detail=getattr(argv, "task_detail", None),
                    node_name=getattr(argv, "node_name", "CooperativeRun"),
                )

            total = int(settings["count"])
            if not TaskProgress().run(context, progress_argv("start", total)):
                raise RuntimeError("协力演出次数初始化失败")

            def report_progress(_completed: int, expected_total: int) -> None:
                if not TaskProgress().run(
                    context,
                    progress_argv("completed", expected_total),
                ):
                    raise RuntimeError("协力演出次数进度记录失败")

            return CooperativeLiveFlow(
                context,
                settings,
                progress_callback=report_progress,
            ).run()
        except InterruptedError:
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"协力演出失败：{type(exc).__name__}: {exc}"
            record_failure_reason(reason)
            traceback.print_exc()
            print(f"[任务][协力演出][流程][ERROR] {reason}", flush=True)
            return False


@AgentServer.custom_action("CooperativeLiveFinalize")
class CooperativeLiveFinalize(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            if context.tasker.stopping:
                return True
            if not CommonRecover().run(context, argv):
                print("CooperativeLive finalize_warning=演出已完成，主页恢复失败不终止任务", flush=True)
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f"协力演出结束导航失败：{type(exc).__name__}: {exc}"
            traceback.print_exc()
            print(f"CooperativeLive finalize_warning={reason}", flush=True)
            return True
