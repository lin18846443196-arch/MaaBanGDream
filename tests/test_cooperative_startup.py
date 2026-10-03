"""Regression for the 21:14 missed transition and its missing retry path."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca


class CooperativeStartupTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.settings = dict(ca.DEFAULT_SETTINGS, count=1, disconnect_jump_enabled=True)
        self.flow = ca.CooperativeLiveFlow(self.context, self.settings)
        self.clock = Clock()
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        patch.object(ca, "append_current_run_event").start()
        self.flow.visible = Mock(return_value=False)
        self.flow.playfield_detector = Mock(return_value=False)
        self.flow.make_final_cover_entry_resolver = Mock(return_value=None)
        self.flow.capture = Mock(side_effect=AssertionError("slow navigation capture during startup"))
        self.clear = np.full((720, 1280, 3), 60, np.uint8)
        self.black = np.zeros_like(self.clear)

    def test_startup_node_explicitly_overrides_inherited_waits(self):
        nodes = json.loads((ROOT / "resource/pipeline/common.json").read_text(encoding="utf-8"))
        defaults = json.loads((ROOT / "resource/default_pipeline.json").read_text(encoding="utf-8"))
        self.assertEqual(defaults["Default"]["post_delay"], 1000)
        node = nodes["CooperativeStartupRefreshScreen"]
        self.assertEqual((node["pre_delay"], node["post_delay"], node["rate_limit"]), (0, 0, 0))
        self.assertEqual(node["action"], "DoNothing")
        self.assertNotIn("post_delay", nodes["CommonRefreshScreen"])

    def test_fast_capture_uses_callback_safe_node_not_reverse_screencap(self):
        with patch.object(ca, "capture_image", return_value=self.clear) as capture:
            self.assertIs(self.flow.capture_startup(), self.clear)
        capture.assert_called_once_with(self.context, node="CooperativeStartupRefreshScreen")
        self.context.tasker.controller.post_screencap.assert_not_called()

    def test_cancellation_is_preserved(self):
        with patch.object(ca, "capture_image", side_effect=ca.ScreenRefreshCancelled("stop")):
            with self.assertRaises(InterruptedError):
                self.flow.capture_startup()

    def test_normal_navigation_still_uses_normal_capture(self):
        with patch.object(ca, "capture_image", return_value=self.clear) as capture:
            ca.CooperativeLiveFlow.capture(self.flow)
        capture.assert_called_once_with(self.context)

    def test_ready_click_can_observe_short_black_frame(self):
        self.flow.capture_startup = Mock(side_effect=[self.clear, self.black])
        self.flow.template_box = Mock(return_value=(1, 1, 10, 10))
        self.assertEqual(self.flow.watch_ready_delivery_after_click(), "black")
        self.assertEqual(self.flow.capture_startup.call_count, 2)
        self.assertLess(self.clock.now, .1)
        self.flow.capture.assert_not_called()

    def test_ready_delivery_jacket_is_not_thrown_away(self):
        self.flow.capture_startup = Mock(side_effect=[self.clear, self.clear])
        self.flow.template_box = Mock(return_value=None)
        self.assertEqual(self.flow.watch_ready_delivery_after_click(), "button-gone")
        result = object()
        resolver = Mock()
        resolver.observe.side_effect = [None, result]
        self.flow.make_final_cover_entry_resolver.return_value = resolver
        with patch.object(ca, "update_live_run") as update:
            self.assertEqual(self.flow.watch_member_exit_before_black(), "final-cover")
        self.assertEqual(resolver.observe.call_count, 2)
        self.assertEqual(self.flow.capture_startup.call_count, 2)
        self.assertIs(update.call_args.kwargs["startup_final_cover_resolution"], result)
        self.assertIsNone(self.flow._ready_delivery_image)

    def test_transition_watcher_catches_short_window(self):
        def capture():
            self.clock.advance(.01)
            return self.black if .08 <= self.clock.now <= .16 else self.clear
        self.flow.capture_startup = Mock(side_effect=capture)
        self.assertEqual(self.flow.watch_member_exit_before_black(timeout=.5), "black")
        self.assertLess(self.clock.now, .2)

    def test_missed_startup_is_recoverable_without_mid_song_input(self):
        self.flow.recover_failed_live_exit = Mock()
        self.flow.play = Mock()
        with self.assertRaises(ca.CooperativeStartupRetry):
            self.flow.jump_after_startup_failure("missed black transition")
        self.flow.recover_failed_live_exit.assert_called_once()
        self.flow.play.assert_not_called()

    def test_unconfirmed_network_restore_is_still_fatal(self):
        self.flow.recover_failed_live_exit = Mock(side_effect=ca.JumpOutUnavailable("restore failed"))
        with self.assertRaises(ca.JumpOutUnavailable):
            self.flow.jump_after_startup_failure("missed")

    def test_run_attempt_does_not_capture_stale_page_after_recovery(self):
        self.flow.enter_room = Mock(side_effect=ca.CooperativeStartupRetry("recovered"))
        with self.assertRaises(ca.CooperativeStartupRetry):
            self.flow.run_attempt()
        self.flow.capture.assert_not_called()

    def test_startup_retry_uses_same_bounded_loop_and_success_count(self):
        self.flow.run_attempt = Mock(side_effect=[ca.CooperativeStartupRetry("missed"), True])
        self.flow.recover_after_play_failure = Mock()
        self.flow.progress_callback = Mock()
        self.flow.navigate_completed_result = Mock(return_value=False)
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_count, 2)
        self.flow.recover_after_play_failure.assert_called_once()
        self.flow.progress_callback.assert_called_once_with(1, 1)
        self.flow.navigate_completed_result.assert_called_once_with(replay=False)

    def test_repeated_startup_failure_cannot_retry_forever(self):
        self.flow.run_attempt = Mock(side_effect=ca.CooperativeStartupRetry("missed"))
        self.flow.recover_after_play_failure = Mock()
        with self.assertRaises(ca.CooperativeStartupRetry):
            self.flow.run()
        self.assertEqual(self.flow.run_attempt.call_count, 4)
        self.assertEqual(self.flow.recover_after_play_failure.call_count, 3)

    def test_stop_never_initiates_startup_recovery(self):
        self.context.tasker.stopping = True
        self.flow.recover_failed_live_exit = Mock()
        with self.assertRaises(InterruptedError):
            self.flow.jump_after_startup_failure("missed")
        self.flow.recover_failed_live_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
