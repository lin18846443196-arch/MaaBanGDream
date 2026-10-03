"""Challenge playback uses the shared engine and owns failed-live recovery."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import ScreenRefreshCancelled, capture_image
    from ..task_reporting import latest_failure_reason, record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, capture_image
    from task_reporting import latest_failure_reason, record_failure_reason

from .live_session import append_current_run_event, current_live_run
from .native_prearm import discard_prearmed_backend
from .profile_play_action import RealtimeProfilePlay
from .team_recovery import TeamRecoveryFailed, restart_team_game

ROOT = Path(__file__).resolve().parents[2]
LABEL = "\u6311\u6218\u6f14\u51fa"
LIFE_FAILURE = "\u6f14\u51fa\u5931\u8d25\uff1a\u751f\u547d\u503c\u5f52\u96f6"
_LOCK = threading.Lock()
_DISCONNECT_JUMP_ENABLED = False
HOME_RECOVERY = {
    "home_node": "ChallengeHomeMarker",
    "modal_cancel_nodes": ["QuitConfirmCancel"],
    "click_nodes": [
        "AutoLiveLoginTap", "AutoLiveLoginNext", "AutoLiveCommonClose",
        "AutoLiveStorySkipConfirmLarge", "AutoLiveStorySkipConfirm",
        "AutoLiveStorySkip", "AutoLiveStoryMenu",
    ],
    "escape_interval_ms": 1500, "escape_timeout_ms": 60000,
    "home_stable_ms": 400,
    "restart_limit": 1, "restart_wait_ms": 5000, "startup_grace_ms": 12000,
    "login_start_node": "AutoLiveLoginScreenMarker",
    "login_start_target": [640, 635], "login_marker_priority_attempts": 3,
    "escape_after_login_start": True,
    "package": "com.bilibili.star.bili", "login_tap_target": [640, 360],
}


def _params(raw):
    result = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError("\u6311\u6218\u6f14\u51fa\u53c2\u6570\u5fc5\u987b\u662f\u5bf9\u8c61")
    return result


def disconnect_jump_enabled() -> bool:
    with _LOCK:
        return _DISCONNECT_JUMP_ENABLED


@AgentServer.custom_action("ChallengeDisconnectJumpConfigure")
class ChallengeDisconnectJumpConfigure(CustomAction):
    def run(self, context, argv) -> bool:
        global _DISCONNECT_JUMP_ENABLED
        if context.tasker.stopping:
            return True
        try:
            enabled = _params(argv.custom_action_param).get("disconnect_jump_enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError("\u65ad\u7f51\u8df3\u8f66\u5f00\u5173\u5fc5\u987b\u662f\u5e03\u5c14\u503c")
            with _LOCK:
                _DISCONNECT_JUMP_ENABLED = enabled
            return True
        except Exception as exc:
            record_failure_reason(f"\u6311\u6218\u6f14\u51fa\u65ad\u7f51\u8df3\u8f66\u914d\u7f6e\u5931\u8d25\uff1a{exc}")
            return False


class ChallengeRecovery:
    """Adapter for bounded UID-only disconnect, restore and game restart."""

    def __init__(self, context, difficulty):
        self.context = context
        self.settings = {"difficulty": difficulty}
        self.phase = "recovery"
        self.network_retries = 0
        self.next_network_click = 0.0
        self.network_failure = None

    @property
    def controller(self):
        return self.context.tasker.controller

    def check_stop(self):
        if self.context.tasker.stopping:
            raise InterruptedError("\u7528\u6237\u5df2\u505c\u6b62\u6311\u6218\u6f14\u51fa")

    @staticmethod
    def clock():
        return time.monotonic()

    def wait(self, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            self.check_stop()
            time.sleep(min(0.1, max(0.0, deadline - self.clock())))
        self.check_stop()

    def capture(self):
        self.check_stop()
        try:
            return capture_image(self.context, node="ChallengeRefreshScreen")
        except ScreenRefreshCancelled as exc:
            raise InterruptedError(str(exc)) from exc

    def click(self, point):
        self.check_stop()
        require_game_foreground(self.controller)
        self.check_stop()
        result = self.controller.post_click(*point).wait()
        if not result.succeeded:
            raise TeamRecoveryFailed("\u6311\u6218\u6f14\u51fa\u70b9\u51fb\u5931\u8d25\uff1a\u63a7\u5236\u5668\u62d2\u7edd\u8f93\u5165")

    def log(self, message):
        print(f"[\u4efb\u52a1][{LABEL}][{self.phase}] {message}", flush=True)

    def recover_home(self, *, just_restarted=False):
        self.check_stop()
        success = CommonRecover().run(
            self.context,
            SimpleNamespace(custom_action_param=json.dumps(HOME_RECOVERY)),
        )
        self.check_stop()
        if not success:
            raise TeamRecoveryFailed("\u6311\u6218\u6f14\u51fa\u6062\u590d\u5931\u8d25\uff1a\u672a\u80fd\u786e\u8ba4\u56de\u5230\u4e3b\u9875")


def handle_pre_live_settings_confirm(context, difficulty):
    flow = ChallengeRecovery(context, difficulty)
    flow.phase = "pre-start"
    flow.wait(1.5)
    image = flow.capture()
    result = context.run_recognition("ChallengePreLiveSettingsConfirm", image)
    for _ in range(3):
        flow.check_stop()
        if result is None or not result.hit:
            return
        box = result.box
        if box is None:
            raise RuntimeError("\u5f00\u6f14\u8bbe\u7f6e\u786e\u8ba4\u5f39\u7a97\u7f3a\u5c11\u53ef\u4fe1\u6309\u94ae\u4f4d\u7f6e")
        x, y = int(box.x + box.w // 2), int(box.y + box.h // 2)
        if box.w <= 0 or box.h <= 0 or not (600 <= x < 980 and 520 <= y < 700):
            raise RuntimeError("\u5f00\u6f14\u8bbe\u7f6e\u786e\u8ba4\u6309\u94ae\u4f4d\u7f6e\u5f02\u5e38\uff0c\u5df2\u505c\u6b62\u70b9\u51fb")
        flow.click((x, y))
        flow.wait(0.25)
        image = flow.capture()
        result = context.run_recognition("ChallengePreLiveSettingsConfirm", image)
    flow.check_stop()
    if result is not None and result.hit:
        raise RuntimeError("\u5f00\u6f14\u8bbe\u7f6e\u786e\u8ba4\u5f39\u7a97\u70b9\u51fb 3 \u6b21\u540e\u4ecd\u672a\u6d88\u5931")


def _same_run(before, after) -> bool:
    return bool(
        before is not None and after is not None
        and before.run_id and before.run_id == after.run_id
        and before.mode == "challenge" and after.mode == "challenge"
    )


def _record_recovery(status, reason):
    try:
        append_current_run_event(ROOT, "recovery", status, details={
            "mode": "challenge", "reason": reason,
        })
    except Exception as exc:
        print(f"ChallengeProfilePlay recovery_evidence_failed={exc}", flush=True)


@AgentServer.custom_action("ChallengeProfilePlay")
class ChallengeProfilePlay(CustomAction):
    def run(self, context, argv) -> bool:
        if context.tasker.stopping:
            return True
        try:
            params = _params(argv.custom_action_param)
            before = current_live_run()
            if (
                before is None or not before.run_id or before.mode != "challenge"
                or not before.prepared_for_play or before.play_completed
            ):
                raise RuntimeError("\u6311\u6218\u6f14\u51fa\u7f3a\u5c11\u5f53\u524d\u5c40\u51c6\u5907\u51ed\u636e")
            requested = str(params.get("difficulty", "Easy"))
            if str(before.requested_difficulty or before.difficulty).casefold() != requested.casefold():
                raise RuntimeError("\u6311\u6218\u6f14\u51fa\u8bf7\u6c42\u96be\u5ea6\u4e0e\u5f53\u524d\u5c40\u51c6\u5907\u51ed\u636e\u51b2\u7a81")
            enabled = disconnect_jump_enabled()
            params = dict(params, run_mode="challenge")
            if enabled:
                params.update(life_depleted_jump_request=True, defer_failed_exit=True,
                              propagate_failure=True)
            else:
                params.update(life_depleted_jump_request=False, defer_failed_exit=False)
            # Failure text must belong to this invocation before it can trigger recovery.
            record_failure_reason("")
            handle_pre_live_settings_confirm(context, before.difficulty)
            if context.tasker.stopping:
                return True
            play_error = None
            try:
                success = RealtimeProfilePlay().run(context, SimpleNamespace(
                    custom_action_param=json.dumps(params),
                    task_detail=getattr(argv, "task_detail", None),
                ))
            except Exception as exc:
                success, play_error = False, exc
            if context.tasker.stopping:
                return True
            after = current_live_run()
            same_run = _same_run(before, after)
            stats = getattr(play_error, "realtime_stats", None)
            cleanup_failed = bool(stats is not None and (
                stats.cleanup_failed or (stats.native_report or {}).get("release_confirmed") is False
                or (getattr(stats, "engine_mode", None) == "native"
                    and (stats.native_report or {}).get("release_confirmed") is not True)
            ))
            life_failure = bool(
                same_run and not cleanup_failed and (
                    after.disconnect_jump_requested
                    or (not success and latest_failure_reason() == LIFE_FAILURE)
                    or (stats is not None and (stats.life_failed or stats.life_depleted))
                )
            )
            if enabled and life_failure:
                reason = "\u6311\u6218\u6f14\u51fa\u751f\u547d\u5f52\u96f6\uff0c\u5df2\u9000\u51fa\u5e76\u6062\u590d\u7f51\u7edc\uff1b\u672c\u5c40\u672a\u8ba1\u5165\u5b8c\u6210\u6b21\u6570"
                _record_recovery("started", reason)
                discard_prearmed_backend("challenge-failed-live-exit")
                flow = ChallengeRecovery(context, after.difficulty)
                restart_team_game(flow, disconnect=True)
                _record_recovery("completed", reason)
                record_failure_reason(reason)
                return False
            if play_error is not None:
                raise play_error
            if not success or not same_run or not after.play_completed or after.disconnect_jump_requested:
                raise RuntimeError(latest_failure_reason() or "\u6311\u6218\u6f14\u51fa\u672a\u80fd\u786e\u8ba4\u5f53\u524d\u5c40\u6f14\u594f\u5b8c\u6210")
            return True
        except InterruptedError:
            if context.tasker.stopping:
                return True
            record_failure_reason("\u6311\u6218\u6f14\u51fa\u6062\u590d\u88ab\u4e2d\u65ad")
            return False
        except Exception as exc:
            reason = f"\u6311\u6218\u6f14\u51fa\u5931\u8d25\uff1a{type(exc).__name__}: {exc}"
            _record_recovery("failed", reason)
            record_failure_reason(reason)
            print(f"ChallengeProfilePlay failed={reason}", flush=True)
            return False
