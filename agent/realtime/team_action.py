"""普通公开团队演出：自动满员匹配，每局结算后回主页。"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

try:
    from ..common_recover import CommonRecover
    from ..foreground_guard import require_game_foreground
    from ..screen_refresh import ScreenRefreshCancelled, ScreenRefreshInterrupted, capture_image, observe_captured_images, filter_captured_images
    from ..task_reporting import TaskProgress, record_failure_reason
except ImportError:
    from common_recover import CommonRecover
    from foreground_guard import require_game_foreground
    from screen_refresh import ScreenRefreshCancelled, ScreenRefreshInterrupted, capture_image, observe_captured_images, filter_captured_images
    from task_reporting import TaskProgress, record_failure_reason

from .cooperative_action import cooperative_profile_preflight
from .difficulty_action import RealtimeDifficultySelect
from .formal_preflight import RealtimeFormalPreflight
from .live_visual_gate import live_performance_mode_is_off
from .game_effect_settings_action import RealtimeGameSpeedSettingsGate
from .performance_settings_action import RealtimePerformanceSettingsGate
from .profile_play_action import RealtimeProfilePlay
from .live_session import current_live_run, reset_live_run, update_live_run
from .native_prearm import discard_prearmed_backend
from .final_cover import FinalCoverResolver
from .chart_repository import LocalChartRepository
from .playfield_monitor import PlayfieldDetector
from .song_identity import identify_final_song, UNKNOWN_SONG_ID
from .song_title_ocr import recognize_song_title, FINAL_COVER_TITLE_ROI
from .result_navigation import advance_result_cadence, handle_story_page
from .team_recognition import TeamRecognizer, DIFFICULTY_TARGETS, SONG_LEVEL_ROI, SONG_TITLE_ROI
from .vision_io import imwrite_unicode
from .debug_recorder import RealtimeDebugRecorder
from .team_recovery import TeamRecoveryFailed, restart_team_game
from .team_loading import TeamLoadingBudget

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = {
    'difficulty': 'Expert', 'count': 1, 'wait_timeout_seconds': 180,
    'max_retries': 2, 'debug_recording': False, 'diagnostic_trace': True,
}
_SETTINGS = dict(DEFAULT_SETTINGS)
_LOCK = threading.Lock()
ENVIRONMENT = {'require_profile': True, 'dpi': 240, 'game_fps': 60, 'render_quality': 'standard'}
RECOVERY = {
    'home_node': 'TeamHomeMarker', 'modal_cancel_nodes': ['QuitConfirmCancel'],
    'click_nodes': ['AutoLiveLoginTap', 'AutoLiveLoginNext', 'AutoLiveCommonClose',
                    'AutoLiveStorySkipConfirmLarge', 'AutoLiveStorySkipConfirm',
                    'AutoLiveStorySkip', 'AutoLiveStoryMenu'],
    'escape_interval_ms': 1500, 'escape_timeout_ms': 60000,
    'home_stable_ms': 400,
    'restart_limit': 1, 'restart_wait_ms': 5000, 'startup_grace_ms': 12000,
    'login_start_node': 'AutoLiveLoginScreenMarker', 'login_start_target': [640, 635],
    'login_marker_priority_attempts': 3, 'escape_after_login_start': True,
    'package': 'com.bilibili.star.bili', 'login_tap_target': [640, 360],
}


def action_args(params):
    return SimpleNamespace(custom_action_param=json.dumps(params, ensure_ascii=False))


def configure_team_settings(params):
    with _LOCK:
        candidate = dict(DEFAULT_SETTINGS if params.get('reset') else _SETTINGS)
        candidate.update({k: v for k, v in params.items() if k in DEFAULT_SETTINGS})
        if candidate['difficulty'] not in DIFFICULTY_TARGETS:
            raise ValueError('团队演出首版支持 Easy / Normal / Hard / Expert')
        for name, minimum, maximum in [('count', 0, 999), ('wait_timeout_seconds', 30, 600), ('max_retries', 0, 5)]:
            raw = candidate[name]
            value = int(raw)
            if isinstance(raw, bool) or str(raw) != str(value) or not minimum <= value <= maximum:
                raise ValueError(f'{name} 必须是 {minimum} 到 {maximum} 的整数')
            candidate[name] = value
        for name in ('debug_recording', 'diagnostic_trace'):
            if not isinstance(candidate[name], bool):
                raise ValueError(f'{name} 必须是布尔值')
        _SETTINGS.clear()
        _SETTINGS.update(candidate)
        return dict(candidate)


def current_team_settings():
    with _LOCK:
        return dict(_SETTINGS)


def team_play_params(settings):
    return {
        **ENVIRONMENT, 'difficulty': settings['difficulty'], 'run_mode': 'team',
        'settings_gate_required': True, 'duration_seconds': 600,
        'startup_timeout_seconds': 60, 'wait_for_completion': True,
        'completion_missing_frames': 30, 'require_completion': True,
        'continue_after_life_depleted': True, 'life_depleted_jump_request': True,
        'propagate_failure': True, 'defer_failed_exit': True,
        'confirm_final_cover': True, 'native_prearm_deferred': True,
        'final_cover_timeout_seconds': 60, 'save_result_frame': True,
        # The outer flow owns settlement through Home, including team results.
        'defer_result_collection': True,
        'debug_recording': settings['debug_recording'],
        'diagnostic_trace': settings['diagnostic_trace'],
    }


class TeamUnavailable(RuntimeError):
    """No public team entry: retrying the same task cannot enable the event."""


class TeamNetworkInterrupted(ScreenRefreshInterrupted):
    """A network modal invalidated in-flight preparation or exceeded retries."""


class TeamLiveFlow:
    def __init__(self, context, settings, *, progress=None):
        self.context, self.settings, self.progress = context, dict(settings), progress
        self.recognizer = TeamRecognizer()
        self.last_image = None
        self.phase = 'entry'
        self.completed_ids = set()
        self.next_error_check = 0.0
        self.next_network_click = 0.0
        self.network_retries = 0
        self.network_failure = None

    @staticmethod
    def clock():
        return time.monotonic()

    @property
    def controller(self):
        # Nested Maa tasks may replace the reverse controller proxy.
        return self.context.tasker.controller

    def check_stop(self):
        if self.context.tasker.stopping:
            raise InterruptedError('用户已停止团队演出')

    def wait(self, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(.05, max(0, deadline-time.monotonic())))
        self.check_stop()

    def capture(self):
        self.check_stop()
        try:
            node = ('ResultRefreshScreen' if self.phase == 'result' else
                    'CommonRefreshScreen' if self.phase in {'entry','home'} else 'TeamRefreshScreen')
            self.last_image = capture_image(self.context, node=node)
        except ScreenRefreshCancelled as exc:
            raise InterruptedError(str(exc)) from exc
        if self.last_image.shape != (720, 1280, 3):
            raise RuntimeError('团队演出需要 1280×720 游戏画面')
        return self.last_image

    def click(self, point):
        self.check_stop()
        require_game_foreground(self.controller)
        self.check_stop()
        self.controller.post_click(*point).wait()

    def box(self, image, node):
        self.check_stop()
        result = self.context.run_recognition(node, image)
        return result.box if result and result.hit else None

    def click_box(self, box):
        self.click((int(box.x+box.w//2), int(box.y+box.h//2)))

    def invoke(self, action, params, reason):
        self.check_stop()
        success = action.run(self.context, action_args(params))
        self.check_stop()
        # Some shared actions catch exceptions and return False. Keep the
        # original modal failure so they cannot silently resume stale inputs.
        if self.network_failure:
            raise TeamNetworkInterrupted(self.network_failure)
        if not success:
            raise RuntimeError(reason)

    def log(self, state):
        print(f'[任务][团队演出][{self.phase}] {state}', flush=True)

    def evidence(self, reason):
        try:
            folder = ROOT / 'debug/team-live'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            if self.last_image is not None:
                imwrite_unicode(folder / f'{stamp}-{self.phase}.png', self.last_image)
            (folder / f'{stamp}.json').write_text(json.dumps({
                'phase': self.phase, 'reason': reason,
                'run_id': getattr(current_live_run(), 'run_id', None),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as exc:
            self.log(f'诊断保存失败：{exc}')

    def handle_error(self, image, screen, *, check_room_error=True):
        now = time.monotonic()
        if screen.state in {'connect_error', 'network_unavailable'}:
            if now >= self.next_network_click and screen.button:
                if self.network_retries >= 3:
                    raise RuntimeError('团队连接错误，重试3次仍未恢复')
                self.click(screen.button)
                self.network_retries += 1
                self.next_network_click = now+2
                self.log(f'连接错误重试 {self.network_retries}/3')
            return True
        if check_room_error and now >= self.next_error_check:
            self.next_error_check = now+1
            if self.box(image, 'TeamRoomError') is not None:
                raise RuntimeError('团队成员退出、房间解散或匹配失败')
        return False

    def guard_network_modal(self, image):
        """Intercept every shared task capture, including nested settings/login.

        Never run inside native play or intentional disconnection. No background
        thread may take screenshots or click while another phase owns the input.
        """
        if self.phase in {'play', 'recovery'}:
            return image
        if self.network_failure:
            raise TeamNetworkInterrupted(self.network_failure)
        interrupted_phase = self.phase
        deadline = self.clock()+20
        handled = False
        try:
            # Raw captures inside this context bypass the filter itself, while
            # retaining the existing recording observer.
            with filter_captured_images(None):
                while True:
                    self.check_stop()
                    screen = self.recognizer.network_modal(image)
                    if screen is None:
                        break
                    self.last_image = image
                    handled = True
                    if self.clock() >= deadline:
                        raise TeamNetworkInterrupted('网络弹窗20秒内未消失')
                    self.handle_error(image, screen, check_room_error=False)
                    self.wait(.25)
                    image = capture_image(self.context, node='TeamRefreshScreen')
            if handled:
                self.last_image = image
                self.log(f'网络弹窗已关闭，重新检查 {interrupted_phase} 页面')
                if interrupted_phase in {'prepare','loading'}:
                    raise TeamNetworkInterrupted('准备／加载期间网络中断，放弃本局身份并重新入房')
                if interrupted_phase in {'entry','matching','settings','result'}:
                    raise TeamNetworkInterrupted('导航／匹配／设置／结算期间网络中断，重新从主页进入流程')
            return image
        except InterruptedError:
            raise
        except Exception as exc:
            self.network_failure = str(exc)
            raise TeamNetworkInterrupted(str(exc)) from exc

    def restart_after_failure(self, reason, *, disconnect):
        self.check_stop()
        discard_prearmed_backend('team-recovery')
        self.log(f'异常恢复：{reason}')
        try:
            restart_team_game(self, disconnect=disconnect)
        except InterruptedError:
            raise
        except Exception as exc:
            raise TeamRecoveryFailed(f'团队异常恢复失败：{exc}') from exc

    def recover_home(self, *, just_restarted=False):
        self.check_stop()
        discard_prearmed_backend('team-return-home')
        previous_phase = self.phase
        self.phase = 'home'
        try:
            self.invoke(CommonRecover(), {**RECOVERY, 'app_just_started': just_restarted},
                        '团队流程无法恢复游戏主页')
            # This phase can safely accept a fresh login/home screenshot after
            # a network modal. Other phases must discard their stale state.
            image = self.capture()
            if self.box(image, 'TeamHomeMarker') is None:
                raise RuntimeError('主页恢复后未确认到主页，停止重新入房')
        finally:
            self.phase = previous_phase

    def navigate_team_home(self):
        deadline, next_click, clicks = time.monotonic()+30, 0.0, 0
        while time.monotonic() < deadline:
            image = self.capture()
            screen = self.recognizer.observe(image)
            if self.handle_error(image, screen, check_room_error=False):
                self.wait(.25)
                continue
            if screen.state == 'home':
                return
            if time.monotonic() >= next_click:
                # Team title template is specific: never use cooperative color fallback.
                point = self.recognizer.point(image, 'entry')
                if point:
                    self.click(point)
                else:
                    close = self.box(image, 'TeamNavigationClose')
                    if close:
                        self.click_box(close)
                    elif self.box(image, 'TeamHomeMarker'):
                        home_live = self.box(image, 'TeamHomeLive')
                        if home_live:
                            self.click_box(home_live)
                    elif self.box(image, 'TeamLiveSelectMarker'):
                        # OCR fallback handles minor title rendering changes.
                        entry = self.box(image, 'TeamEntryOCR')
                        if entry:
                            self.click_box(entry)
                clicks += 1
                next_click = time.monotonic()+2
                if clicks >= 12:
                    break
            self.wait(.25)
        raise TeamUnavailable('未找到可用的团队演出入口／首页，请确认团队活动正在开放')

    def join_room(self):
        deadline, next_click, clicks = time.monotonic()+30, 0.0, 0
        while time.monotonic() < deadline:
            image = self.capture()
            screen = self.recognizer.observe(image)
            if self.handle_error(image, screen):
                self.wait(.25)
                continue
            if screen.state in {'lobby', 'full', 'matching', 'random_song', 'prepare'}:
                return
            if screen.state == 'home' and time.monotonic() >= next_click:
                if not self.recognizer.box(image, 'normal_selected'):
                    self.click((1020, 174))
                elif screen.button:
                    self.click(screen.button)
                clicks += 1
                if clicks >= 5:
                    raise RuntimeError('团队普通房间入房点击未生效')
                next_click = time.monotonic()+2
            self.wait(.25)
        raise RuntimeError('30秒内未能进入团队普通匹配')

    def wait_for_preparation(self):
        self.phase = 'matching'
        timeout = int(self.settings['wait_timeout_seconds'])
        deadline, hard_deadline = time.monotonic()+timeout, time.monotonic()+timeout*2+60
        stage, streak, previous = 'members', 0, ''
        while time.monotonic() < min(deadline, hard_deadline):
            image = self.capture()
            screen = self.recognizer.observe(image)
            if self.handle_error(image, screen):
                self.wait(.25)
                continue
            streak = streak+1 if screen.state == previous else 1
            if screen.state != previous:
                self.log(screen.state)
            previous = screen.state
            if screen.state == 'prepare' and screen.button:
                return
            if screen.state in {'ready_wait', 'loading'}:
                raise RuntimeError('未验证本局难度就已进入准备／加载，停止本次启动')
            if screen.state in {'home', 'entry'} and streak >= 2:
                raise RuntimeError('团队匹配已退回入口')
            if screen.state == 'matching' and stage == 'members':
                stage, deadline = 'opponents', time.monotonic()+timeout
            if screen.state == 'random_song' and stage != 'song':
                stage, deadline = 'song', time.monotonic()+60
            # Deliberately no input to the lobby: pink start is enabled below capacity.
            self.wait(.2)
        raise RuntimeError(f'团队等待超时（{stage}），本次未开始演奏')

    def require_preparation(self):
        image = self.capture()
        screen = self.recognizer.observe(image)
        if screen.state != 'prepare' or not screen.button:
            raise RuntimeError('团队最终确认页已改变或倒计时结束，停止准备输入')
        return image

    def prepare(self):
        self.phase = 'prepare'
        started = time.monotonic()
        self.require_preparation()
        self.invoke(RealtimeDifficultySelect(), {
            'difficulty': self.settings['difficulty'], 'mode': 'team',
            'page_guard': 'TeamPrepareTitle',
            'refresh_node': 'TeamRefreshScreen',
            'max_attempts': 3, 'verify_delay_seconds': .2,
            'identity_read_attempts': 2, 'identity_retry_delay_seconds': .1,
            'difficulty_targets': DIFFICULTY_TARGETS,
            'song_level_roi': SONG_LEVEL_ROI, 'song_title_roi': SONG_TITLE_ROI,
            'song_identity': False, 'debug_recording': self.settings['debug_recording'],
        }, '团队难度选择／识别失败')
        run = current_live_run()
        if not run or not run.prepared_for_play or run.difficulty != self.settings['difficulty']:
            raise RuntimeError('团队缺少本局实际难度证据')
        self.require_preparation()
        self.invoke(RealtimePerformanceSettingsGate(), {
            **ENVIRONMENT, 'difficulty': run.difficulty, 'defer_native_prearm': True,
            'refresh_node': 'TeamRefreshScreen',
        }, '团队流速复核失败')
        self.require_preparation()
        # Songs supporting 3D/MV show the same carousel as single live. Switch
        # it OFF before reading Cut in: that position otherwise contains avatars.
        self.invoke(RealtimeFormalPreflight(), {
            'page_guard': 'TeamPrepareTitle', 'cut_in_off_node': 'TeamCutInOff',
            'refresh_node': 'TeamRefreshScreen',
            'mode_settle_seconds': .25, 'cut_in_settle_seconds': .25,
            'check_auto_live': False, 'check_performance_mode': True,
        }, '团队 Cut in 关闭失败')
        self.require_safe_preparation()
        self.log(f'难度、流速、演出模式 OFF 及 Cut in 已确认，准备耗时 {time.monotonic()-started:.2f}s')

    def require_safe_preparation(self, image=None):
        image = self.require_preparation() if image is None else image
        if (not live_performance_mode_is_off(image)
                or not self.recognizer.box(image, 'cut_in_off')):
            raise RuntimeError('开演前演出模式／Cut in 状态改变，停止准备输入')
        return image

    def ready_and_wait_for_cover(self):
        self.phase = 'loading'
        run = current_live_run()
        if run is None or not run.prepared_for_play:
            raise RuntimeError('缺少本局准备证据')
        resolver = FinalCoverResolver(
            difficulty=run.difficulty, observed_level=run.song_level,
            observed_title=run.song_title,
            observed_title_confidence=float(run.song_title_confidence or 0),
            repository=LocalChartRepository(ROOT / 'resource/charts'),
        )
        budget = TeamLoadingBudget(time.monotonic(), self.settings['wait_timeout_seconds'])
        next_click, clicks, next_loading_log = 0.0, 0, 0.0
        acknowledged, playfield_frames, next_title_read = False, 0, 0.0
        playfield = PlayfieldDetector()
        while True:
            image = self.capture()
            screen = self.recognizer.observe(image)
            now = time.monotonic()
            reason = budget.observe(now, screen.state, screen.loading_progress)
            if reason:
                raise RuntimeError(f'{reason}，最终封面未确认：{resolver.last_reason}')
            if self.handle_error(image, screen):
                self.wait(.15)
                continue
            if screen.state in {'ready_wait', 'loading'}:
                acknowledged = True
                if now >= next_loading_log:
                    progress = ('未知' if budget.high_water is None else
                                '/'.join(str(value) for value in budget.high_water))
                    self.log(f'等待成员加载：已等待 {now-budget.member_started:.0f}s，'
                             f'无进展 {now-budget.last_progress:.0f}s，进度约 {progress}；'
                             f'无进展上限 {int(budget.member_timeout)}s，'
                             f'成员总上限 {int(2*budget.member_timeout)}s')
                    next_loading_log = now+15
            if screen.state == 'prepare' and screen.button:
                if acknowledged:
                    raise RuntimeError('准备被取消或本局退回最终确认页')
                if time.monotonic() >= next_click:
                    if clicks >= 3:
                        raise RuntimeError('准备完毕点击3次仍未送达')
                    self.require_safe_preparation(image)
                    self.click(screen.button)
                    self.log('已点击准备完毕，等待成员加载与最终封面')
                    clicks += 1
                    next_click = time.monotonic()+2
            if screen.state in {'home', 'entry', 'lobby', 'matching'}:
                raise RuntimeError('团队开演前房间已退出或重新匹配')
            if clicks and screen.state == 'unknown':
                # Do not hand over on black alone. Team has several black transitions.
                if (time.monotonic() >= next_title_read
                        and identify_final_song(image).song_id != UNKNOWN_SONG_ID):
                    next_title_read = time.monotonic()+.5
                    reading = recognize_song_title(image, roi=FINAL_COVER_TITLE_ROI)
                    if reading:
                        resolver.refresh_observed_title(reading.text, reading.confidence)
                resolution = resolver.observe(image)
                if resolution:
                    update_live_run(startup_final_cover_image=image.copy(),
                                    startup_final_cover_resolution=resolution)
                    self.log('最终歌曲封面已确认，交给实时演奏引擎')
                    return
                playfield_frames = playfield_frames+1 if playfield(image) else 0
                if playfield_frames >= 2:
                    raise RuntimeError('已进入轨道但未确认最终封面，禁止从歌曲中段启动')
            self.wait(.08)

    def play(self):
        self.phase = 'play'
        run = current_live_run()
        run_id = None if run is None else run.run_id
        success = RealtimeProfilePlay().run(self.context, action_args(team_play_params(self.settings)))
        self.check_stop()
        after = current_live_run()
        if after is not None and after.run_id == run_id and after.disconnect_jump_requested:
            raise RuntimeError('团队演奏生命归零，已停止触控并请求断网逃生')
        if not success or after is None or after.run_id != run_id or not after.play_completed:
            raise RuntimeError('团队实时演奏未确认本局完成')
        return after

    def return_home(self):
        self.phase = 'result'
        deadline, next_click, home_hits = time.monotonic()+180, 0.0, 0
        back_next, fallback_steps = False, 0
        saved = False
        previous_state, next_wait_log = None, time.monotonic()+15

        def before_result_input():
            self.check_stop()
            require_game_foreground(self.controller)
            self.check_stop()

        while time.monotonic() < deadline:
            image = self.capture()
            screen = self.recognizer.observe(image, results=True)
            if screen.state != previous_state:
                self.log(f'结算页面：{screen.state}，按钮={"可用" if screen.button else "等待"}')
                previous_state = screen.state
            if not screen.button and time.monotonic() >= next_wait_log:
                self.log(f'结算尚未回主页：{screen.state}，共享推进 {fallback_steps} 次')
                next_wait_log = time.monotonic()+15
            if self.handle_error(image, screen, check_room_error=False):
                home_hits = 0
                back_next = False
                next_click = time.monotonic()+.8
                self.wait(.25)
                continue
            quit_box = self.box(image, 'QuitConfirmCancel')
            if quit_box:
                self.click_box(quit_box)
                home_hits = 0
                back_next = False
                next_click = time.monotonic()+.8
                self.wait(.25)
                continue
            home_hits = home_hits+1 if self.box(image, 'TeamHomeMarker') else 0
            if home_hits >= 2:
                self.log('已连续确认主页，结算结束')
                return True
            if home_hits:
                self.wait(.25)
                continue
            if screen.state == 'pggbm' and screen.button and not saved:
                run = current_live_run()
                try:
                    folder = ROOT / 'screencap'
                    folder.mkdir(parents=True, exist_ok=True)
                    imwrite_unicode(folder / f'team-result-{run.run_id}.png', image)
                    saved = True
                except Exception as exc:
                    self.log(f'成绩截图保存失败：{exc}')
            if time.monotonic() >= next_click:
                # Story confirmations need explicit controls. Check them even
                # when a result template remains visible behind the overlay.
                if handle_story_page(image, recognise=self.box, click=self.click,
                                     stopping=lambda: bool(self.context.tasker.stopping)):
                    back_next = False
                    next_click = time.monotonic()+.5
                elif screen.button:
                    self.log(f'结算推进：{screen.state}')
                    self.click(screen.button)
                    back_next = False
                    next_click = time.monotonic()+.8
                else:
                    # Reuse single/cooperative settlement inputs for rank-ups,
                    # daily/event rewards and animations without a template.
                    # Send only ONE input, then capture again before the next:
                    # BACK may already have returned Home or opened a story.
                    back_next = advance_result_cadence(
                        lambda: self.controller, back_next=back_next,
                        before_input=before_result_input,
                        phase=screen.state, log_prefix='TeamResult',
                    )
                    fallback_steps += 1
                    next_click = time.monotonic()+.8
            self.wait(.2)
        raise RuntimeError('团队结算180秒内未返回主页')

    def save_round_report(self, run, settled, warning=None, *, returned_home=True):
        try:
            folder = ROOT / 'screencap'
            folder.mkdir(parents=True, exist_ok=True)
            payload = {'mode': 'team', 'run_id': run.run_id, 'play_completed': True,
                       'settlement_finished': settled, 'returned_home': returned_home,
                       'completed_count': len(self.completed_ids), 'warning': warning,
                       'run': run.to_mapping()}
            path = folder / f'team-round-{run.run_id}.json'
            temporary = path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        except Exception as exc:
            self.log(f'本局报告保存失败：{exc}')

    def run_attempt(self):
        self.phase = 'entry'
        self.network_retries = 0
        self.network_failure = None
        self.next_network_click = self.next_error_check = 0.0
        discard_prearmed_backend('team-new-round')
        reset_live_run(mode='team', difficulty=self.settings['difficulty'])
        self.recover_home()
        error = cooperative_profile_preflight(self.context, self.settings['difficulty'])
        self.check_stop()
        if error:
            raise TeamUnavailable(error)
        self.phase = 'settings'
        self.invoke(RealtimeGameSpeedSettingsGate(), {
            **ENVIRONMENT, 'entry_mode': 'home', 'difficulty': self.settings['difficulty'],
        }, '团队主页流速检查失败')
        self.phase = 'entry'
        self.navigate_team_home()
        self.join_room()
        self.wait_for_preparation()
        self.prepare()
        self.ready_and_wait_for_cover()
        return self.play()

    def run(self):
        with filter_captured_images(self.guard_network_modal):
            return self._run()

    def _run(self):
        total, completed = int(self.settings['count']), 0
        retries = {'entry': 0, 'play': 0}
        try:
            while total == 0 or completed < total:
                self.check_stop()
                self.log(f'第 {completed+1}/{total or "无限"} 局')
                try:
                    run = self.run_attempt()
                except (InterruptedError, TeamUnavailable, TeamRecoveryFailed):
                    raise
                except Exception as exc:
                    self.check_stop()
                    bucket = 'play' if self.phase == 'play' else 'entry'
                    self.evidence(str(exc))
                    stats = getattr(exc, 'realtime_stats', None)
                    if stats is not None and (stats.cleanup_failed or
                            (getattr(stats, 'native_report', None) or {}).get('release_confirmed') is False):
                        raise TeamRecoveryFailed('Native 触点清理未确认，停止自动重新入房') from exc
                    if retries[bucket] >= self.settings['max_retries']:
                        if self.phase in {'prepare','loading','play'}:
                            # Even at the limit, leave the failed live cleanly;
                            # do not open another room after this recovery.
                            self.restart_after_failure(str(exc), disconnect=True)
                        raise RuntimeError(f'团队{bucket}阶段重试已达上限：{exc}') from exc
                    retries[bucket] += 1
                    self.log(f'异常重试 {retries[bucket]}/{self.settings["max_retries"]}：{exc}')
                    if self.phase in {'prepare','loading','play'} or isinstance(exc,TeamNetworkInterrupted) or self.network_failure:
                        self.restart_after_failure(str(exc), disconnect=self.phase in {'prepare','loading','play'})
                    continue
                # Count exactly once before result navigation. Navigation failures must
                # never cause a replay of a song the engine has already completed.
                if run.run_id in self.completed_ids:
                    raise RuntimeError('团队演出重复的本局标识，停止重复计数')
                self.completed_ids.add(run.run_id)
                completed += 1
                retries = {'entry': 0, 'play': 0}
                # Persist completion before any settlement input. A stop/recovery
                # failure must not erase the completed song from diagnostics.
                self.save_round_report(run, False, '结算进行中', returned_home=False)
                self.log(f'演奏已完成 {completed}/{total or "无限"}，继续领取奖励并返回主页')
                if self.progress:
                    self.progress(completed, total)
                settled, warning = True, None
                try:
                    self.return_home()
                except (InterruptedError, ScreenRefreshCancelled):
                    self.save_round_report(run, False, '用户在结算期间停止', returned_home=False)
                    raise
                except Exception as exc:
                    warning, settled = str(exc), False
                    self.evidence(warning)
                    self.log(f'本局已完成，结算恢复：{warning}')
                    try:
                        if isinstance(exc, TeamNetworkInterrupted) or self.network_failure:
                            self.restart_after_failure(warning, disconnect=False)
                        else:
                            self.recover_home()
                    except Exception as recovery_error:
                        self.save_round_report(run, False,
                            f'{warning}；主页恢复失败：{recovery_error}', returned_home=False)
                        raise
                self.save_round_report(run, settled, warning)
                if total == 0 or completed < total:
                    self.log(f'本局已回主页，下一局 {completed+1}/{total or "无限"} 将重新进入团队演出')
            return True
        finally:
            discard_prearmed_backend('team-task-exit')


@AgentServer.custom_action('TeamLiveConfigure')
class TeamLiveConfigure(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return True
        try:
            configure_team_settings(json.loads(argv.custom_action_param or '{}'))
            return True
        except Exception as exc:
            record_failure_reason(f'团队演出配置无效：{exc}')
            return False


@AgentServer.custom_action('TeamLiveFlow')
class TeamLiveAction(CustomAction):
    def run(self, context, argv):
        flow = None
        recorder = None
        try:
            if context.tasker.stopping:
                return True
            settings = current_team_settings()

            def report(completed, total):
                report_argv = SimpleNamespace(
                    custom_action_param=json.dumps({
                        'task_name': 'TeamLive', 'label': '团队演出', 'total': total,
                        'phase': 'restore', 'completed': completed, 'next_started': False,
                    }, ensure_ascii=False),
                    task_detail=getattr(argv, 'task_detail', None),
                    node_name=getattr(argv, 'node_name', 'TeamRun'),
                )
                if not TaskProgress().run(context, report_argv):
                    print('TeamLive progress_warning=进度显示更新失败', flush=True)

            report(0, settings['count'])
            flow = TeamLiveFlow(context, settings, progress=report)
            if settings['debug_recording'] or settings['diagnostic_trace']:
                recorder = RealtimeDebugRecorder(
                    ROOT/'debug/recordings', session_kind='team-flow',
                    video_fps=10, video_enabled=settings['debug_recording'],
                    session_metadata={'mode': 'team-flow', 'settings': settings},
                )
                flow.log(f'导航诊断：{recorder.output_dir}')

            def observe(image, node):
                # CommonRecover also uses capture_image: preserve startup evidence
                # even when the task never reaches the first performance.
                flow.last_image = image
                if recorder is not None and flow.phase != 'play':
                    recorder.record_phase(image.copy(), time.monotonic(), flow.phase,
                        diagnostics=[{'kind': 'navigation-capture', 'node': node}])

            with observe_captured_images(observe):
                return flow.run()
        except (InterruptedError, ScreenRefreshCancelled):
            return True
        except Exception as exc:
            if context.tasker.stopping:
                return True
            reason = f'团队演出失败：{type(exc).__name__}: {exc}'
            if flow:
                flow.evidence(reason)
            record_failure_reason(reason)
            traceback.print_exc()
            print(f'[任务][团队演出][ERROR] {reason}', flush=True)
            return False
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as exc:
                    print(f'TeamLive recorder_warning={exc}', flush=True)
