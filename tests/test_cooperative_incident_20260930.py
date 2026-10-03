"""Recorded popup false positives and bounded restriction recovery; no device."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.native_play import NativeStartPhotogate
from realtime.prepare_popup import CooperativePreparePopupDetector
from realtime.vision_io import imread_unicode

FIXTURES = ROOT / "tests/fixtures/cooperative-20260930"


class PopupIncidentTests(unittest.TestCase):
    def test_real_popup_is_blocked_but_recorded_notes_are_not(self):
        gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed")
        for name, expected in (
            ("waiting-members", True), ("popup-gone", False),
            ("notes-false-popup", False), ("notes-after-false-popup", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(gate._popup_detector(
                    imread_unicode(FIXTURES / (name + ".png"))), expected)
        self.assertTrue(CooperativePreparePopupDetector()(
            imread_unicode(FIXTURES / "notes-false-popup.png")))

    def test_recorded_notes_no_longer_clear_a_stable_start_baseline(self):
        gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed",
                                   playfield_detector=lambda image: True)
        gate._startup_life = None
        baseline = imread_unicode(FIXTURES / "popup-gone.png")
        for step in range(60):
            gate.observe(baseline, step / 60)
        gate.observe(imread_unicode(FIXTURES / "notes-false-popup.png"), 1.)
        self.assertEqual(gate.prepare_popup_frames, 0)
        self.assertEqual(gate.prepare_popup_blocked_events, 0)


class RestrictionIncidentTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()),
                          run_recognition=Mock(return_value=NS(
                              hit=True, best_result=NS(text="功能限制剩余时间24秒"))))
        self.flow = ca.CooperativeLiveFlow(self.context, dict(ca.DEFAULT_SETTINGS))
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        self.flow.click = Mock()
        self.flow.pipeline_box = Mock(return_value=None)
        self.popup = imread_unicode(FIXTURES / "restriction-24s.png")
        self.clear = imread_unicode(FIXTURES / "live-select.png")

    def test_actual_restriction_is_acknowledged_then_waited_out(self):
        self.assertTrue(self.flow.wait_for_cooperative_restriction(
            self.popup, remaining_budget=300))
        self.flow.click.assert_called_once_with((639, 490))
        self.assertEqual(self.clock.now, 26)

    def test_other_pages_are_never_acknowledged(self):
        for image in (self.clear, imread_unicode(FIXTURES / "waiting-members.png")):
            self.assertFalse(self.flow.wait_for_cooperative_restriction(
                image, remaining_budget=300))
        self.flow.click.assert_not_called()
        self.context.run_recognition.assert_not_called()

    def test_countdown_supports_minutes_and_whitespace(self):
        self.context.run_recognition.return_value.best_result.text = "功能限制剩余时间：1分 24秒"
        self.flow.wait_for_cooperative_restriction(self.popup, remaining_budget=300)
        self.assertEqual(self.clock.now, 86)

    def test_unknown_countdown_gets_bounded_recheck(self):
        self.context.run_recognition.return_value = None
        self.flow.wait_for_cooperative_restriction(self.popup, remaining_budget=300)
        self.assertEqual(self.clock.now, 30)

    def test_long_restriction_stops_with_explicit_reason(self):
        self.context.run_recognition.return_value.best_result.text = "功能限制剩余时间1小时"
        with self.assertRaisesRegex(RuntimeError, "游戏协力功能暂时受限"):
            self.flow.wait_for_cooperative_restriction(self.popup, remaining_budget=300)
        self.flow.click.assert_not_called()

    def test_stop_during_wait_prevents_reentry(self):
        def sleep(seconds):
            self.clock.advance(seconds)
            self.context.tasker.stopping = True
        with patch.object(ca.time, "sleep", sleep):
            with self.assertRaises(InterruptedError):
                self.flow.wait_for_cooperative_restriction(self.popup, remaining_budget=300)
        self.assertLessEqual(self.clock.now, .5)
        self.assertEqual(self.flow.click.call_count, 1)

    def test_reentry_after_cooldown_does_not_use_up_navigation_timeout(self):
        self.flow.capture = Mock(side_effect=[self.popup, self.clear, self.clear])
        self.flow.visible = Mock(side_effect=[False, True])
        self.context.run_recognition.return_value.best_result.text = "功能限制剩余时间1分钟"
        self.flow.navigate_to_cooperative_room_selection("home")
        self.assertGreaterEqual(self.clock.now, 62)
        self.assertEqual(self.flow.click.call_count, 2)
        self.assertEqual(self.flow.click.call_args_list[0].args[0], (639, 490))

    def test_persistent_restriction_cannot_extend_deadline_forever(self):
        self.flow.capture = Mock(return_value=self.popup)
        with self.assertRaisesRegex(RuntimeError, "5分钟"):
            self.flow.navigate_to_cooperative_room_selection("home")
        self.assertLessEqual(self.clock.now, 300)
        self.assertGreater(self.clock.now, 260)

    def test_first_entry_uses_restriction_aware_navigation(self):
        self.flow.navigate_to_cooperative_room_selection = Mock()
        self.flow.select_normal_room = Mock()
        self.flow.enter_room()
        self.flow.navigate_to_cooperative_room_selection.assert_called_once_with("entry")
        self.flow.select_normal_room.assert_called_once()


if __name__ == "__main__":
    unittest.main()
