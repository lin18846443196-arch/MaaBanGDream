"""MuMu 前台恢复不重复唤起游戏；协力仍先恢复旧断网规则。"""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import ROOT
from test_common_recover import Context as RecoveryContext, argv as recovery_args
import task_reporting as reporting
import common_recover as cr
from foreground_guard import GAME_PACKAGE
from realtime import cooperative_action as ca

PACKAGE = GAME_PACKAGE


class PassiveForegroundRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.context.tasker.controller.info = {
            "config": {"extras": {"mumu": {"enable": True}}},
            "screencap_methods": 64,
        }

    def test_foreground_mumu_game_is_not_started_again(self):
        with patch.object(cr, "_package_running", return_value=True), \
             patch.object(cr, "foreground_package", return_value=PACKAGE):
            self.assertEqual(cr._prepare_game(self.context, PACKAGE), (True, False))
        self.context.tasker.controller.post_start_app.assert_not_called()
        self.context.tasker.controller.post_stop_app.assert_not_called()

    def test_background_game_and_missing_process_still_start_normally(self):
        for running, foreground in ((True, "app.lawnchair"), (False, None), (None, None)):
            with self.subTest(running=running, foreground=foreground), \
                 patch.object(cr, "_package_running", return_value=running), \
                 patch.object(cr, "foreground_package", return_value=foreground):
                self.context.tasker.controller.post_start_app.reset_mock()
                self.assertEqual(cr._prepare_game(self.context, PACKAGE), (True, True))
                self.context.tasker.controller.post_start_app.assert_called_once_with(PACKAGE)

    def test_home_recovery_uses_current_mumu_frame_without_launch_or_geometry_poll(self):
        context = RecoveryContext({"HomeMarker": [True]})
        context.tasker.controller.info = self.context.tasker.controller.info
        with patch.object(context.tasker.controller, "post_shell",
                          wraps=context.tasker.controller.post_shell) as shell:
            self.assertTrue(cr.CommonRecover().run(context, recovery_args()))
        self.assertEqual(context.refreshes, 1)
        self.assertEqual(context.tasker.controller.starts, [])
        self.assertEqual(context.tasker.controller.stops, [])
        self.assertEqual(context.tasker.controller.keys, [])
        self.assertTrue(all(call.args[0] != "dumpsys display" for call in shell.call_args_list))

    def test_user_stop_prevents_foreground_and_background_launch(self):
        self.context.tasker.stopping = True
        for foreground in (PACKAGE, "app.lawnchair"):
            with self.subTest(foreground=foreground), \
                 patch.object(cr, "_package_running", return_value=True), \
                 patch.object(cr, "foreground_package", return_value=foreground):
                self.assertEqual(cr._prepare_game(self.context, PACKAGE), (False, False))
        self.context.tasker.controller.post_start_app.assert_not_called()


class CooperativeStartupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.argv = NS(custom_action_param="{}", task_detail=NS(task_id=99001))
        self.settings = dict(ca.DEFAULT_SETTINGS, count=0, disconnect_jump_enabled=True)
        self.addCleanup(reporting.clear_states)

    def run_startup(self, gate, recover):
        flow = Mock()
        flow.check_escape_available.return_value = gate
        with patch.object(ca, "current_cooperative_settings", return_value=self.settings), \
             patch.object(ca, "CooperativeLiveFlow", return_value=flow), \
             patch.object(ca, "CommonRecover", return_value=NS(run=recover)):
            return ca.CooperativeLiveRecover().run(self.context, self.argv)

    def test_old_escape_is_restored_before_any_login_capture(self):
        sequence = []
        gate = Mock()
        gate.restore_stale_escapes.side_effect = lambda: sequence.append("restore") or True
        self.assertTrue(self.run_startup(gate, lambda *a: sequence.append("recover") or True))
        self.assertEqual(sequence, ["restore", "recover"])
        state = reporting._states[99001]
        self.assertEqual((state.total, state.current, state.completed), (0, 0, 0))
        start = NS(custom_action_param=json.dumps({"total": 0, "phase": "start"}),
                   task_detail=self.argv.task_detail, node_name="CooperativeRun")
        self.context.get_hit_count = lambda name: 1
        with patch.object(reporting, "_visible_log", return_value=True):
            self.assertTrue(reporting.TaskProgress().run(self.context, start))
        self.assertEqual(state.current, 1)

    def test_unconfirmed_network_restore_prevents_login(self):
        gate = Mock(last_error="unexpected chain rule")
        gate.restore_stale_escapes.return_value = False
        recover = Mock()
        self.assertFalse(self.run_startup(gate, recover))
        recover.assert_not_called()
        self.assertIn("unexpected chain rule", reporting.latest_failure_reason())

    def test_pipeline_applies_all_options_before_startup_recovery(self):
        nodes = json.loads((ROOT / "resource/pipeline/cooperative_live.json").read_text(encoding="utf-8"))
        name = "CooperativeProcessConflictGuard"
        seen = []
        while name != "CooperativeSpeedSettingsGate":
            self.assertNotIn(name, seen)
            seen.append(name)
            name = nodes[name]["next"][0]
        self.assertLess(seen.index("CooperativeCountConfigure"), seen.index("CooperativeRecover"))
        self.assertLess(seen.index("CooperativeDisconnectJumpConfigure"), seen.index("CooperativeRecover"))
        self.assertEqual(nodes["CooperativeRecover"]["custom_action"], "CooperativeLiveRecover")


if __name__ == "__main__":
    unittest.main()
