"""Bounded game-only disconnection followed by an unconditional game restart."""
from __future__ import annotations

import uuid

from .cooperative_network import GAME_PACKAGE, GameNetworkGate
from .cooperative_action import (
    CooperativeLiveFlow, DISCONNECT_CONTINUE_INTERRUPT_POINT,
    DISCONNECT_CONFIRM_INTERRUPT_POINT,
)
try:
    from ..foreground_guard import require_game_foreground
    from ..maa_shell_compat import shell_output
except ImportError:
    from foreground_guard import require_game_foreground
    from maa_shell_compat import shell_output


class TeamRecoveryFailed(RuntimeError):
    pass


def maa_shell(controller, args):
    """Maa returns shell stdout, not exit status; append and require a sentinel.

    Args are exclusively the fixed commands emitted by GameNetworkGate, with
    validated UID and our generated chain name. No user-provided shell text.
    """
    marker = '__TEAM_SHELL_RC__'
    command = ' '.join(str(part) for part in args)
    try:
        output = shell_output(controller(),
            '( ' + command + ' ) 2>&1; _team_rc=$?; printf "\\n' + marker + '%s\\n" "$_team_rc"', 8000,
        )
    except Exception as exc:
        return -1, f'{type(exc).__name__}: {exc}'
    body, separator, status = str(output or '').rpartition(marker)
    if not separator:
        return -1, 'ADB shell 未返回退出状态：' + body
    try:
        return int(status.strip()), body.rstrip()
    except ValueError:
        return -1, 'ADB shell 退出状态无效'


def restart_team_game(flow, *, disconnect: bool):
    """Called only after Native cleanup. Restore networking even on user stop.

    Use the cooperative dialog identities, never global Wi-Fi/airplane mode.
    Unsupported root or changed dialog layout falls back to force-stop/restart.
    Only a confirmed network restore permits relaunch and another room.
    """
    flow.check_stop()
    # Validate assets before blocking. This class is used only as a recognizer;
    # all captures and inputs stay on this Team flow's current controller.
    recognizer = CooperativeLiveFlow(flow.context, {'difficulty':flow.settings['difficulty']}) if disconnect else None
    gate = GameNetworkGate(lambda args: maa_shell(lambda: flow.controller, args),
                           chain_suffix='team_' + uuid.uuid4().hex[:12]) if disconnect else None
    stopped_app = False
    blocked = False
    flow.phase = 'recovery'
    try:
        if gate is not None:
            blocked = gate.block()
            flow.log('断网逃生：仅阻断游戏网络' if blocked else
                     f'断网控制不可用，改为重启游戏：{gate.last_error}')
            flow.check_stop()
            if blocked:
                require_game_foreground(flow.controller)
                flow.controller.post_click_key(3).wait()
                flow.wait(.6)
                flow.controller.post_start_app(GAME_PACKAGE).wait()
                for name, point, timeout in (
                    ('disconnect_continue_body', DISCONNECT_CONTINUE_INTERRUPT_POINT, 12.),
                    ('disconnect_confirm_body', DISCONNECT_CONFIRM_INTERRUPT_POINT, 8.),
                ):
                    deadline = flow.clock() + timeout
                    matched = False
                    while flow.clock() < deadline:
                        image = flow.capture()
                        if recognizer.visible(image, name, .93):
                            flow.click(point)
                            matched = True
                            break
                        flow.wait(.25)
                    if not matched:
                        flow.log(f'断网弹窗未出现：{name}，直接关闭游戏后重启')
                        break
        flow.check_stop()
        result = flow.controller.post_stop_app(GAME_PACKAGE).wait()
        if not result.succeeded:
            raise TeamRecoveryFailed('无法确认游戏已关闭，停止自动恢复')
        stopped_app = True
    finally:
        if gate is not None:
            had_rules = gate.cleanup_required
            restored = False
            for _ in range(3):
                try:
                    restored = gate.restore()
                except Exception as exc:
                    flow.log(f'恢复游戏网络异常：{exc}')
                if restored:
                    break
            if not restored:
                raise TeamRecoveryFailed('游戏网络恢复未确认，禁止重开房间；请检查模拟器网络')
            flow.log('游戏网络已恢复' if blocked or had_rules else
                     '未写入断网规则，无需恢复网络')
    flow.check_stop()
    if not stopped_app:
        raise TeamRecoveryFailed('游戏重启流程未完成')
    flow.wait(.5)
    result = flow.controller.post_start_app(GAME_PACKAGE).wait()
    if not result.succeeded:
        raise TeamRecoveryFailed('游戏启动失败，停止自动恢复')
    flow.wait(2.)
    flow.network_retries = 0
    flow.next_network_click = 0.
    flow.network_failure = None
    flow.phase = 'entry'
    flow.recover_home(just_restarted=True)
    flow.log('游戏重启并确认主页，重新开始本局流程')
