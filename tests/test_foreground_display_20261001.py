"""MuMu app panels must not inherit the desktop or another panel's focus."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import Clock, ROOT
import common_recover as cr
import foreground_guard as fg


GAME = fg.GAME_PACKAGE
DESKTOP = "app.lawnchair"
OTHER = "com.example.browser"


def windows(game_current=GAME, game_focused=GAME, top=0):
    def record(package):
        return f"Window{{a u0 {package}/.MainActivity}}" if package else "null"
    return f"""WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=3 (organized)
    mCurrentFocus=null
    mFocusedApp=ActivityRecord{{a u0 com.example.other/.MainActivity t78}}
  Display: mDisplayId=2 (organized)
    mCurrentFocus={record(game_current)}
    mFocusedApp={record(game_focused)}
  Display: mDisplayId=0 (organized)
    mFocusedApp=ActivityRecord{{a u0 {DESKTOP}/.LawnchairLauncher t2}}
    mCurrentFocus=Window{{b u0 {DESKTOP}/app.lawnchair.LawnchairLauncher}}
  WINDOW MANAGER WINDOWS (dumpsys window windows)
    mTopFocusedDisplayId={top}
"""


def activities(resumed=GAME, *, background_only=False):
    active = "" if background_only else f"topResumedActivity=ActivityRecord{{a u0 {resumed}/.PermissionActivity t77}}"
    return f"""ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Display #0 (activities from top to bottom):
    topResumedActivity=ActivityRecord{{b u0 {DESKTOP}/.LawnchairLauncher t2}}
Display #2 (activities from top to bottom):
    * Hist #0: ActivityRecord{{a u0 {GAME}/.MainActivity t77}}
    {active}
  ResumedActivity: ActivityRecord{{b u0 {DESKTOP}/.LawnchairLauncher t2}}
  ActivityTaskSupervisor state:
"""


def controller(*, enabled=True, screencap=64):
    return NS(info={
        "config": {"extras": {"mumu": {"enable": enabled}}},
        "screencap_methods": screencap,
    })


class ForegroundDisplayTests(unittest.TestCase):
    def test_standard_uses_top_focused_display_not_first_panel(self):
        self.assertEqual(fg._parse_foreground(windows()), DESKTOP)
        self.assertEqual(fg._parse_foreground(windows(top=2)), GAME)

    def test_current_window_takes_priority_over_remembered_activity(self):
        self.assertEqual(fg._parse_foreground(windows(OTHER, GAME, top=2)), OTHER)

    def test_known_null_current_window_does_not_allow_stale_activity(self):
        self.assertIsNone(fg._parse_foreground(windows(None, GAME, top=2)))

    def test_unscoped_multiple_displays_are_not_inferred_from_text_order(self):
        self.assertIsNone(fg._parse_foreground(windows().replace("mTopFocusedDisplayId=0", "")))

    def test_legacy_dump_prioritizes_current_window(self):
        output = (
            f"  mFocusedApp=ActivityRecord{{a u0 {GAME}/.MainActivity t1}}\n"
            f"  mCurrentFocus=Window{{a u0 {OTHER}/.MainActivity}}\n"
        )
        self.assertEqual(fg._parse_foreground(output), OTHER)

    def test_mumu_game_panel_can_be_resumed_while_desktop_owns_global_focus(self):
        self.assertEqual(fg.game_foreground_display(windows(), activities()), 2)
        self.assertEqual(fg._parse_mumu_foreground(windows(), activities()), GAME)

    def test_null_panel_focus_requires_both_resumed_and_focused_game(self):
        self.assertEqual(fg.game_foreground_display(windows(None), activities()), 2)
        self.assertIsNone(fg.game_foreground_display(windows(None, OTHER), activities()))
        self.assertIsNone(fg.game_foreground_display(windows(None), activities(OTHER)))

    def test_game_task_in_background_is_insufficient(self):
        self.assertIsNone(fg.game_foreground_display(windows(), activities(background_only=True)))
        self.assertEqual(fg._parse_mumu_foreground(windows(), activities(background_only=True)), DESKTOP)

    def test_foreign_overlay_on_game_display_blocks_input(self):
        self.assertIsNone(fg.game_foreground_display(windows(OTHER), activities()))

    def test_game_window_without_resumed_confirmation_blocks_input(self):
        self.assertIsNone(fg.game_foreground_display(windows(), activities(OTHER)))

    def test_conflicting_resumed_apps_on_same_panel_are_ambiguous(self):
        dump = activities().replace(
            "  ResumedActivity:",
            f"    Resumed: ActivityRecord{{c u0 {OTHER}/.MainActivity t80}}\n  ResumedActivity:",
        )
        self.assertIsNone(fg.game_foreground_display(windows(), dump))

    def test_multiple_matching_game_panels_are_ambiguous(self):
        extra_window = f"  Display: mDisplayId=5 (organized)\n    mCurrentFocus=Window{{c u0 {GAME}/.MainActivity}}\n"
        window_dump = windows().replace("  WINDOW MANAGER WINDOWS", extra_window + "  WINDOW MANAGER WINDOWS")
        activity_dump = activities().replace(
            "  ResumedActivity:",
            f"Display #5 (activities from top to bottom):\n    topResumedActivity=ActivityRecord{{c u0 {GAME}/.MainActivity t81}}\n  ResumedActivity:",
        )
        self.assertIsNone(fg.game_foreground_display(window_dump, activity_dump))

    def test_mumu_rules_require_enabled_extras_screenshot_method(self):
        for ctrl in (controller(enabled=False), controller(screencap=1), controller(enabled="true"), NS(info={})):
            with self.subTest(controller=ctrl):
                self.assertFalse(fg.mumu_extras_active(ctrl))
        self.assertTrue(fg.mumu_extras_active(controller()))

    def test_real_foreground_api_applies_panel_rule_only_to_extras(self):
        def read(_controller, *command):
            return activities() if "activity" in command else windows()
        with patch.object(fg, "_read_dump", side_effect=read):
            self.assertEqual(fg.foreground_package(controller()), GAME)
            self.assertEqual(fg.foreground_package(controller(screencap=1)), DESKTOP)

    def test_known_missing_focus_cannot_fall_back_to_stale_unscoped_activity(self):
        with patch.object(fg, "_read_dump", side_effect=[windows(None, GAME, top=2), windows()]) as read:
            self.assertIsNone(fg.foreground_package(controller(screencap=1)))
        self.assertEqual(read.call_count, 1)

    def test_shell_dump_uses_sdk_compatibility_helper(self):
        with patch.object(fg, "shell_output", return_value=windows()) as shell:
            self.assertEqual(fg.foreground_package(controller(enabled=False)), DESKTOP)
        shell.assert_called_once_with(unittest.mock.ANY, "dumpsys window", 5000)

    def test_failed_reverse_shell_uses_direct_read_only_adb(self):
        ctrl = controller(enabled=False)
        ctrl.info.update(adb_path="adb.exe", adb_serial="127.0.0.1:16384")
        with patch.object(fg, "shell_output", side_effect=RuntimeError("reverse shell unavailable")), \
             patch.object(fg.subprocess, "run", return_value=NS(stdout=windows())) as adb:
            self.assertEqual(fg.foreground_package(ctrl), DESKTOP)
        self.assertEqual(adb.call_args.args[0], ["adb.exe", "-s", "127.0.0.1:16384", "shell", "dumpsys", "window"])

    def test_overlay_still_raises_foreground_guard(self):
        with patch.object(fg, "_read_dump", side_effect=[windows(OTHER), activities()]):
            with self.assertRaises(fg.ForegroundAppMismatch):
                fg.require_game_foreground(controller())


class RecoveryFailureReasonTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.argv = NS(custom_action_param={"restart_limit": 0, "startup_grace_ms": 0})

    def test_foreground_failure_preserves_specific_reason(self):
        def capture(*_args, **_kwargs):
            self.clock.advance(31)
            return "desktop"
        with patch.object(cr, "_prepare_game", return_value=(True, True)), \
             patch.object(cr, "capture_image", side_effect=capture), \
             patch.object(cr, "require_game_foreground", side_effect=fg.ForegroundAppMismatch("blocked")), \
             patch.object(cr, "foreground_package", return_value=DESKTOP), \
             patch.object(cr.time, "monotonic", self.clock.time), \
             patch.object(cr, "record_failure_reason") as reason:
            self.assertFalse(cr.CommonRecover().run(self.context, self.argv))
        self.assertIn(DESKTOP, reason.call_args.args[0])
        self.assertIn(GAME, reason.call_args.args[0])
        self.context.tasker.controller.post_click.assert_not_called()
        self.context.tasker.controller.post_click_key.assert_not_called()

    def test_callback_exception_preserves_specific_reason(self):
        with patch.object(cr, "_prepare_game", side_effect=RuntimeError("controller unavailable")), \
             patch.object(cr, "record_failure_reason") as reason:
            self.assertFalse(cr.CommonRecover().run(self.context, self.argv))
        self.assertIn("controller unavailable", reason.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
