"""Escape capability checks fail before joining; no device/network commands."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import ROOT
from realtime import cooperative_action as ca
from realtime import team_recovery as recovery
from realtime import adb_root as ar
from realtime.cooperative_network import GameNetworkGate, resolve_game_uid


class EscapeCapabilityTests(unittest.TestCase):
    def test_android15_uid_uses_current_user_when_game_is_not_running(self):
        shell = Mock(side_effect=[
            (0, "appId=10058"), (1, ""),
            (0, "package:com.bilibili.star.bili uid:1010058"),
        ])
        self.assertEqual(resolve_game_uid(shell), 1010058)
        self.assertIn("current", shell.call_args.args[0])

    def test_uid_query_does_not_accept_another_package_or_invalid_uid(self):
        for output in ("package:com.bilibili.star.bili.extra uid:10058",
                       "package:com.bilibili.star.bili uid:0",
                       "package:com.bilibili.star.bili uid:10058;reboot"):
            shell = Mock(side_effect=[(0, "appId=10058"), (1, ""), (0, output)])
            self.assertIsNone(resolve_game_uid(shell), output)

    def test_root_shell_checks_firewall_without_writing_rules(self):
        shell = Mock(side_effect=[(0, "0"), (0, "0"), (0, "-P OUTPUT ACCEPT"),
                                  (0, "owner match options"), (0, "userId=10058")])
        gate = GameNetworkGate(shell)
        self.assertTrue(gate.check_available())
        self.assertFalse(gate.cleanup_required)
        self.assertEqual([call.args[0] for call in shell.call_args_list], [
            ("id", "-u"), ("id", "-u"), ("iptables", "-S", "OUTPUT"),
            ("iptables", "-m", "owner", "-h"),
            ("dumpsys", "package", "com.bilibili.star.bili"),
        ])
        self.assertTrue(gate.restore())
        self.assertEqual(shell.call_count, 5)

    def test_nonroot_without_su_reports_actual_failure_before_rule_writes(self):
        shell = Mock(side_effect=[(0, "2000"), (127, "su: inaccessible or not found")])
        gate = GameNetworkGate(shell)
        self.assertFalse(gate.check_available())
        self.assertIn("rc=127", gate.last_error)
        self.assertIn("su: inaccessible or not found", gate.last_error)
        self.assertIn("adb root", gate.last_error)
        self.assertFalse(gate.cleanup_required)
        self.assertEqual(shell.call_count, 2)

    def test_su_path_is_checked_and_reused(self):
        commands = []
        def shell(args):
            commands.append(tuple(args))
            if args == ("id", "-u"):
                return 0, "2000"
            if args[0] == "dumpsys":
                return 0, "userId=10058"
            if args[0] == "su":
                return 0, "0" if args[2] == '"id -u"' else "available"
            raise AssertionError(args)
        gate = GameNetworkGate(shell)
        self.assertTrue(gate.check_available())
        self.assertEqual(sum(args == ("id", "-u") for args in commands), 1)
        self.assertTrue(all(args[0] == "su" for args in commands[1:-1]))

    def test_missing_game_uid_fails_preflight_without_changing_rules(self):
        shell = Mock(side_effect=[(0, "0"), (0, "0"), (0, "ok"),
                                  (0, "owner available"), (0, "no uid"),
                                  (1, ""), (0, "")])
        gate = GameNetworkGate(shell)
        self.assertFalse(gate.check_available())
        self.assertIn("游戏 UID", gate.last_error)
        self.assertFalse(gate.cleanup_required)

    def test_successful_su_exit_without_root_uid_is_rejected(self):
        gate = GameNetworkGate(Mock(side_effect=[(0, "2000"), (0, "2000")]))
        self.assertFalse(gate.check_available())
        self.assertFalse(gate.cleanup_required)

    def test_firewall_or_uid_extension_failure_is_reported(self):
        for responses, text in (
            ([(0, "0"), (0, "0"), (1, "Permission denied")], "OUTPUT"),
            ([(0, "0"), (0, "0"), (0, "ok"), (1, "owner not available")], "UID"),
        ):
            with self.subTest(text=text):
                gate = GameNetworkGate(Mock(side_effect=responses))
                self.assertFalse(gate.check_available())
                self.assertIn(text, gate.last_error)
                self.assertFalse(gate.cleanup_required)

    def test_checked_shell_captures_stderr_and_preserves_nonzero_exit(self):
        with patch.object(recovery, "shell_output",
                          return_value="su: not found\n__TEAM_SHELL_RC__127") as shell:
            code, message = recovery.maa_shell(lambda: object(), ("su", "-c", '"id -u"'))
        self.assertEqual(code, 127)
        self.assertIn("su: not found", message)
        self.assertIn('( su -c "id -u" ) 2>&1;', shell.call_args.args[1])


class EscapeEntryTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.context.tasker.controller.post_connection.return_value.wait.return_value = NS(succeeded=True)
        self.flow = ca.CooperativeLiveFlow(
            self.context, dict(ca.DEFAULT_SETTINGS, disconnect_jump_enabled=True))
        self.flow.navigate_to_cooperative_room_selection = Mock()
        self.flow.select_normal_room = Mock()

    def test_enabled_but_unavailable_never_joins_or_restarts(self):
        gate = Mock(last_error="root rc=127", check_available=Mock(return_value=False),
                    root_access_available=False)
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            with patch.object(ca, "enable_adb_root", return_value=(False, "root denied")):
                with self.assertRaisesRegex(ca.JumpOutUnavailable, "root denied"):
                    self.flow.enter_room()
        self.flow.navigate_to_cooperative_room_selection.assert_not_called()
        self.flow.select_normal_room.assert_not_called()
        self.context.tasker.controller.post_stop_app.assert_not_called()
        gate.block.assert_not_called()

    def test_missing_su_automatically_enables_adb_root_before_room_entry(self):
        first = Mock(check_available=Mock(return_value=False), root_access_available=False)
        second = Mock(check_available=Mock(return_value=True))
        with patch.object(ca, "GameNetworkGate", side_effect=[first, second]), \
             patch.object(ca, "enable_adb_root", return_value=(True, "uid=0")) as enable, \
             patch.object(ca, "discard_prearmed_backend") as discard:
            self.flow.enter_room()
        enable.assert_called_once_with(self.flow.controller, stopping=self.flow.stopped)
        discard.assert_called_once_with("cooperative-adb-root")
        second.check_available.assert_called_once()
        self.flow.select_normal_room.assert_called_once()
        self.context.tasker.controller.post_connection.assert_called_once()
        first.block.assert_not_called()
        second.block.assert_not_called()

    def test_adb_root_success_still_requires_maa_permission_verification(self):
        first = Mock(check_available=Mock(return_value=False), root_access_available=False)
        second = Mock(check_available=Mock(return_value=False), last_error="Maa still uid2000")
        with patch.object(ca, "GameNetworkGate", side_effect=[first, second]), \
             patch.object(ca, "enable_adb_root", return_value=(True, "uid=0")), \
             patch.object(ca, "discard_prearmed_backend"):
            with self.assertRaisesRegex(ca.JumpOutUnavailable, "Maa still uid2000"):
                self.flow.enter_room()
        self.flow.select_normal_room.assert_not_called()

    def test_firewall_failure_does_not_restart_adbd(self):
        gate = Mock(check_available=Mock(return_value=False),
                    root_access_available=True, last_error="iptables unavailable")
        with patch.object(ca, "GameNetworkGate", return_value=gate), \
             patch.object(ca, "enable_adb_root") as enable:
            with self.assertRaisesRegex(ca.JumpOutUnavailable, "iptables unavailable"):
                self.flow.enter_room()
        enable.assert_not_called()

    def test_failed_maa_reconnection_cannot_enter_room(self):
        first = Mock(check_available=Mock(return_value=False), root_access_available=False)
        self.context.tasker.controller.post_connection.return_value.wait.return_value = NS(succeeded=False)
        with patch.object(ca, "GameNetworkGate", return_value=first), \
             patch.object(ca, "enable_adb_root", return_value=(True, "uid=0")), \
             patch.object(ca, "discard_prearmed_backend"):
            with self.assertRaisesRegex(ca.JumpOutUnavailable, "连接恢复失败"):
                self.flow.enter_room()
        self.flow.select_normal_room.assert_not_called()

    def test_available_escape_allows_normal_entry(self):
        gate = Mock(check_available=Mock(return_value=True))
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            self.flow.enter_room()
        gate.check_available.assert_called_once()
        gate.block.assert_not_called()
        self.flow.select_normal_room.assert_called_once()

    def test_disabled_escape_skips_firewall_check(self):
        self.flow.settings["disconnect_jump_enabled"] = False
        with patch.object(ca, "GameNetworkGate") as gate:
            self.flow.enter_room()
        gate.assert_not_called()
        self.flow.select_normal_room.assert_called_once()

    def test_unavailable_escape_is_fatal_without_retrying_a_song(self):
        self.flow.settings["count"] = 1
        self.flow.run_attempt = Mock(side_effect=ca.JumpOutUnavailable("断网能力不可用"))
        self.flow.recover_after_play_failure = Mock()
        with patch.object(ca, "record_failure_reason"):
            self.assertFalse(self.flow.run())
        self.flow.run_attempt.assert_called_once()
        self.flow.recover_after_play_failure.assert_not_called()


class AdbRootTests(unittest.TestCase):
    def setUp(self):
        from test_team_live import Clock
        self.clock = Clock()
        self.controller = NS(info={"adb_path": "X:/emulator/adb.exe", "adb_serial": "target:1234"})
        self.stopped = False
        self.addCleanup(patch.stopall)
        patch.object(ar.time, "monotonic", self.clock.time).start()
        patch.object(ar.time, "sleep", self.clock.advance).start()

    def run_root(self):
        return ar.enable_adb_root(self.controller, stopping=lambda: self.stopped)

    def result(self, code=0, out="", err=""):
        return NS(returncode=code, stdout=out, stderr=err)

    def test_exact_endpoint_is_used_and_transport_recovery_is_verified(self):
        results = [self.result(out="restarting adbd as root"),
                   self.result(1, err="device offline"), self.result(out="0\n")]
        with patch.object(ar.subprocess, "run", side_effect=results) as run:
            self.assertTrue(self.run_root()[0])
        self.assertEqual(run.call_args_list[0].args[0],
                         ["X:/emulator/adb.exe", "-s", "target:1234", "root"])
        self.assertEqual(run.call_args.args[0][-3:], ["shell", "id", "-u"])

    def test_production_build_rejection_is_not_reported_as_success(self):
        with patch.object(ar.subprocess, "run",
                          return_value=self.result(out="adbd cannot run as root in production builds")) as run:
            enabled, message = self.run_root()
        self.assertFalse(enabled)
        self.assertIn("production builds", message)
        self.assertEqual(run.call_count, 1)

    def test_nonroot_reply_is_bounded(self):
        results = [self.result(out="restarting"), self.result(out="2000")]
        with patch.object(ar.subprocess, "run",
                          side_effect=lambda *a, **kw: results.pop(0) if len(results) > 1 else results[0]):
            enabled, message = self.run_root()
        self.assertFalse(enabled)
        self.assertIn("2000", message)
        self.assertLessEqual(self.clock.now, 15)

    def test_stop_never_attempts_root(self):
        self.stopped = True
        with patch.object(ar.subprocess, "run") as run:
            with self.assertRaises(InterruptedError):
                self.run_root()
        run.assert_not_called()

    def test_missing_endpoint_is_not_guessed(self):
        self.controller.info = {}
        with patch.object(ar.subprocess, "run") as run:
            self.assertFalse(self.run_root()[0])
        run.assert_not_called()

    def test_transport_exception_preserves_explicit_failure(self):
        with patch.object(ar.subprocess, "run", side_effect=ar.subprocess.TimeoutExpired("adb", 4)):
            enabled, message = self.run_root()
        self.assertFalse(enabled)
        self.assertIn("TimeoutExpired", message)


if __name__ == "__main__":
    unittest.main()
