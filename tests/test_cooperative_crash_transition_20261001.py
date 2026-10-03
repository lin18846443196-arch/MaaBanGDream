"""Replay MuMu startup transitions without invoking its native renderer."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import ROOT, Clock
import capture_transition as ct
import task_reporting as reporting
import common_recover as cr
from realtime import cooperative_action as ca

PACKAGE = ct.GAME_PACKAGE


def window(display=2, focus=PACKAGE):
    return (f"  Display: mDisplayId={display} (organized)\n"
            f"  mCurrentFocus=Window{{x u0 {focus}/.MainActivity}}\n"
            f"  mFocusedApp=ActivityRecord{{x u0 {focus}/.MainActivity}}\n"
            "  Display: mDisplayId=0 (organized)\n"
            "  mCurrentFocus=Window{x u0 app.lawnchair/.Launcher}\n"
            "  mTopFocusedDisplayId=0\n")


def activity(display=2):
    return (f"Display #{display} (activities from top to bottom):\n"
            f"  topResumedActivity=ActivityRecord{{x u0 {PACKAGE}/.MainActivity}}\n")


def dimensions(display=2, width=1280, height=720):
    return (f'mBaseDisplayInfo=DisplayInfo{{"screen", displayId {display}, real 720 x 1280}}\n'
            f'mOverrideDisplayInfo=DisplayInfo{{"screen", displayId {display}, real {width} x {height}}}\n')


class CaptureTransitionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.addCleanup(patch.stopall)
        patch.object(ct.time, "monotonic", self.clock.time).start()
        patch.object(ct.time, "sleep", self.clock.advance).start()

    def probe(self, *, window_fn=None, activity_fn=None, dimensions_fn=None):
        commands = []
        def shell(controller, command, timeout):
            commands.append(command)
            if command == "dumpsys window":
                return (window_fn or (lambda: window()))()
            if command == "dumpsys activity activities":
                return (activity_fn or (lambda: activity()))()
            return (dimensions_fn or (lambda: dimensions()))()
        with patch.object(ct, "mumu_extras_active", return_value=True), \
             patch.object(ct, "shell_output", side_effect=shell):
            ct.wait_for_game_capture_ready(self.context, timeout_seconds=1.5)
        self.context.tasker.controller.post_screencap.assert_not_called()
        self.context.tasker.controller.post_click.assert_not_called()
        self.assertTrue(all(command.startswith("dumpsys ") for command in commands))

    def test_portrait_game_must_rotate_and_remain_stable_before_capture(self):
        self.probe(dimensions_fn=lambda: dimensions(width=720, height=1280)
                   if self.clock.now < .3 else dimensions())
        self.assertGreaterEqual(self.clock.now, .8)

    def test_desktop_landscape_without_game_is_never_ready(self):
        with self.assertRaisesRegex(RuntimeError, "暂停截图"):
            self.probe(window_fn=lambda: window(focus="app.lawnchair"))
        self.assertLess(self.clock.now, 1.7)

    def test_overlay_on_game_display_blocks_capture(self):
        with self.assertRaises(RuntimeError):
            self.probe(window_fn=lambda: window(focus="com.android.permissioncontroller"))

    def test_top_focused_virtual_game_without_resumed_evidence_blocks_capture(self):
        with self.assertRaises(RuntimeError):
            self.probe(window_fn=lambda: window().replace("mTopFocusedDisplayId=0", "mTopFocusedDisplayId=2"),
                       activity_fn=lambda: "")

    def test_top_focused_virtual_game_with_ambiguous_panels_blocks_capture(self):
        extra = f"  Display: mDisplayId=3 (organized)\n  mCurrentFocus=Window{{x u0 {PACKAGE}/.MainActivity}}\n"
        with self.assertRaises(RuntimeError):
            self.probe(window_fn=lambda: window().replace("mTopFocusedDisplayId=0", "mTopFocusedDisplayId=2") + extra,
                       activity_fn=lambda: activity(2) + activity(3))

    def test_standard_display_game_can_use_explicit_global_focus(self):
        self.probe(window_fn=lambda: f"  mCurrentFocus=Window{{x u0 {PACKAGE}/.MainActivity}}\n  mTopFocusedDisplayId=0\n",
                   activity_fn=lambda: "", dimensions_fn=lambda: dimensions(0))

    def test_unscoped_game_focus_cannot_assume_standard_display(self):
        with self.assertRaises(RuntimeError):
            self.probe(window_fn=lambda: f"  mCurrentFocus=Window{{x u0 {PACKAGE}/.MainActivity}}\n",
                       activity_fn=lambda: "", dimensions_fn=lambda: dimensions(0))

    def test_other_display_dimensions_do_not_prove_game_is_landscape(self):
        with self.assertRaises(RuntimeError):
            self.probe(dimensions_fn=lambda: dimensions(4))

    def test_changing_game_display_restarts_stability_wait(self):
        display = lambda: 2 if self.clock.now < .45 else 3
        self.probe(window_fn=lambda: window(display()),
                   activity_fn=lambda: activity(display()),
                   dimensions_fn=lambda: dimensions(display()))
        self.assertGreaterEqual(self.clock.now, .95)

    def test_landscape_resolution_change_restarts_stability_wait(self):
        self.probe(dimensions_fn=lambda: dimensions() if self.clock.now < .45
                   else dimensions(width=1920, height=1080))
        self.assertGreaterEqual(self.clock.now, .95)

    def test_failed_shell_cannot_fall_through_to_renderer(self):
        with patch.object(ct, "mumu_extras_active", return_value=True), \
             patch.object(ct, "shell_output", side_effect=RuntimeError("transport unavailable")):
            with self.assertRaisesRegex(RuntimeError, "transport unavailable"):
                ct.wait_for_game_capture_ready(self.context, timeout_seconds=.3)
        self.context.tasker.controller.post_screencap.assert_not_called()

    def test_stopping_during_transition_is_preserved(self):
        self.context.tasker.stopping = True
        with patch.object(ct, "mumu_extras_active", return_value=True), \
             patch.object(ct, "shell_output") as shell:
            with self.assertRaises(InterruptedError):
                ct.wait_for_game_capture_ready(self.context)
        shell.assert_not_called()

    def test_non_mumu_capture_methods_get_no_added_delay_or_queries(self):
        with patch.object(ct, "mumu_extras_active", return_value=False), \
             patch.object(ct, "shell_output") as shell:
            self.assertTrue(ct.wait_for_game_capture_ready(self.context))
        shell.assert_not_called()
        self.assertEqual(self.clock.now, 0.)

    def test_logical_override_wins_over_portrait_physical_panel(self):
        self.assertTrue(ct.display_is_landscape(dimensions(), 2))
        self.assertFalse(ct.display_is_landscape(dimensions(width=720, height=1280), 2))

    def test_resumed_mumu_game_sets_renderer_app_target_before_capture(self):
        with patch.object(cr, "_package_running", return_value=True), \
             patch.object(cr, "foreground_package", return_value=PACKAGE), \
             patch.object(cr, "mumu_extras_active", return_value=True):
            self.assertEqual(cr._prepare_game(self.context, PACKAGE), (True, True))
        self.context.tasker.controller.post_start_app.assert_called_once_with(PACKAGE)

    def test_recovery_cancellation_at_capture_transition_is_not_failure(self):
        def stop(*args):
            self.context.tasker.stopping = True
            raise InterruptedError("stop")
        with patch.object(cr, "_prepare_game", return_value=(True, True)), \
             patch.object(cr, "wait_for_game_capture_ready", side_effect=stop):
            self.assertTrue(cr.CommonRecover().run(self.context, NS(custom_action_param="{}")))


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
