"""Replay the 20:09 reconnect modal without emulator or network input."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, call, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.debug_recorder import append_lifecycle_event
from realtime.vision_io import imread_unicode


class CooperativeConnectFailedTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = Clock()
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        patch.object(ca, "require_game_foreground").start()
        self.back = patch.object(ca, "accelerated_back").start()
        patch.object(ca, "append_current_run_event").start()
        self.controller = Mock()
        self.context = NS(tasker=NS(stopping=False, controller=self.controller))
        self.flow = ca.CooperativeLiveFlow(self.context, dict(ca.DEFAULT_SETTINGS))
        self.flow.click = Mock()
        self.flow._save_navigation_evidence = Mock()
        self.flow.pipeline_box = Mock(return_value=None)
        self.popup = self.load("tests/fixtures/cooperative-2009/home-connect-failed.png")
        self.home = self.load("tests/fixtures/team/home-circle.png")
        self.entry = self.load("tests/fixtures/cooperative-20260930/live-select.png")
        self.room = self.load("tests/fixtures/cooperative-replay-20261001/room-selection.png")
        self.activity = self.load("tests/fixtures/cooperative-replay-20261001/activity-points.png")
        self.retry_box = self.flow.template_box(self.popup, "connect_failed_retry")
        self.assertIsNotNone(self.retry_box)
        x, y, width, height = self.retry_box
        self.retry_point = (x + width // 2, y + height // 2)

    def load(self, relative):
        image = imread_unicode(ROOT / relative)
        self.assertIsNotNone(image, relative)
        return image

    def screen(self, image):
        current = [image]

        def capture():
            if self.flow.stopped():
                raise InterruptedError("用户已停止任务")
            self.clock.advance(.01)
            return current[0]

        self.flow.capture = capture
        # Reproduce the defect: the recognizer also sees the home behind the
        # foreground connection dialog. The dialog must win over this hit.
        self.flow.pipeline_box = Mock(side_effect=lambda image, node:
            NS(x=1, y=1, w=1, h=1)
            if node == "CooperativeHomeMarker" and
            (image is self.home or image is self.popup) else None)
        return current

    def assert_no_other_input(self):
        self.back.assert_not_called()
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_actual_error_image_recognizes_body_and_bright_retry(self):
        self.assertEqual(self.flow.template_box(self.popup, "connect_failed_body"),
                         (580, 345, 105, 25))
        self.assertEqual(self.retry_point, (769, 526))
        state = ca.ConnectRetryState()
        self.assertTrue(self.flow.handle_connect_failed(self.popup, state, phase="test"))
        self.flow.click.assert_called_once_with(self.retry_point)
        self.assertEqual(state.clicks, 1)
        self.flow._save_navigation_evidence.assert_called_once()
        self.assert_no_other_input()

    def test_real_home_room_result_and_other_modal_are_not_connection_errors(self):
        restriction = self.load("tests/fixtures/cooperative-20260930/restriction-24s.png")
        for name, image in (("home", self.home), ("entry", self.entry),
                            ("room", self.room), ("result", self.activity),
                            ("restriction", restriction)):
            with self.subTest(page=name):
                self.assertFalse(self.flow.handle_connect_failed(
                    image, ca.ConnectRetryState(), phase="test"))
        self.flow.click.assert_not_called()
        self.flow._save_navigation_evidence.assert_not_called()

    def test_retry_button_without_connection_body_sends_no_input(self):
        image = self.popup.copy()
        image[337:378, 572:693] = 100
        self.assertIsNotNone(self.flow.template_box(image, "connect_failed_retry"))
        self.assertFalse(self.flow.handle_connect_failed(
            image, ca.ConnectRetryState(), phase="test"))
        self.flow.click.assert_not_called()

    def test_missing_retry_stops_instead_of_clicking_a_fixed_position(self):
        image = self.popup.copy()
        x, y, width, height = self.retry_box
        image[y - 8:y + height + 8, x - 8:x + width + 8] = 100
        state = ca.ConnectRetryState()
        self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
        self.clock.advance(1.9)
        self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
        self.clock.advance(.1)
        with self.assertRaisesRegex(RuntimeError, "连接失败.*重试按钮"):
            self.flow.handle_connect_failed(image, state, phase="test")
        self.flow.click.assert_not_called()
        self.flow._save_navigation_evidence.assert_called_once()
        self.assert_no_other_input()

    def test_dimmed_retry_cannot_be_clicked_through_another_modal(self):
        image = self.popup.copy()
        x, y, width, height = self.retry_box
        region = image[y - 8:y + height + 8, x - 8:x + width + 8]
        image[y - 8:y + height + 8, x - 8:x + width + 8] = (
            region.astype(np.float32) * .5).astype(np.uint8)
        state = ca.ConnectRetryState()
        self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
        self.clock.advance(2.)
        with self.assertRaisesRegex(RuntimeError, "连接失败.*重试按钮"):
            self.flow.handle_connect_failed(image, state, phase="test")
        self.flow.click.assert_not_called()

    def test_retry_animation_passively_waits_then_clicks_the_bright_button(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                image = self.popup.copy()
                x, y, width, height = self.retry_box
                if missing:
                    image[y - 8:y + height + 8, x - 8:x + width + 8] = 100
                else:
                    image[y - 8:y + height + 8, x - 8:x + width + 8] = (
                        image[y - 8:y + height + 8, x - 8:x + width + 8]
                        .astype(np.float32) * .5).astype(np.uint8)
                self.flow.click.reset_mock()
                state = ca.ConnectRetryState()
                self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
                self.flow.click.assert_not_called()
                self.clock.advance(.5)
                self.assertTrue(self.flow.handle_connect_failed(self.popup, state, phase="test"))
                self.flow.click.assert_called_once_with(self.retry_point)
                self.assertIsNone(state.unclickable_since)
                self.assertEqual(state.clicks, 1)
                self.assert_no_other_input()

    def test_modal_disappearance_resets_animation_timer_but_keeps_click_budget(self):
        image = self.popup.copy()
        x, y, width, height = self.retry_box
        image[y - 8:y + height + 8, x - 8:x + width + 8] = 100
        state = ca.ConnectRetryState(clicks=4)
        self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
        self.clock.advance(1.9)
        self.assertFalse(self.flow.handle_connect_failed(self.home, state, phase="test"))
        self.assertIsNone(state.unclickable_since)
        self.assertEqual(state.clicks, 4)
        self.clock.advance(.2)
        self.assertTrue(self.flow.handle_connect_failed(image, state, phase="test"))
        self.assertEqual(state.unclickable_since, self.clock.now)
        self.assertEqual(state.clicks, 4)
        self.flow.click.assert_not_called()

    def test_retry_interval_and_total_limit_are_bounded(self):
        state = ca.ConnectRetryState()
        for attempt in range(5):
            self.assertTrue(self.flow.handle_connect_failed(self.popup, state, phase="test"))
            self.assertTrue(self.flow.handle_connect_failed(self.popup, state, phase="test"))
            self.assertEqual(self.flow.click.call_count, attempt + 1)
            self.clock.advance(1.)
        with self.assertRaisesRegex(RuntimeError, "连接失败.*5次"):
            self.flow.handle_connect_failed(self.popup, state, phase="test")
        self.assertEqual(self.flow.click.call_count, 5)
        self.assert_no_other_input()

    def test_home_under_modal_is_ignored_then_navigation_continues(self):
        current = self.screen(self.popup)

        def click(point):
            if current[0] is self.popup:
                self.assertEqual(point, self.retry_point)
                current[0] = self.home
            elif current[0] is self.home:
                self.assertEqual(point, ca.HOME_LIVE_POINT)
                current[0] = self.entry
            elif current[0] is self.entry:
                current[0] = self.room
            else:
                self.fail("clicked an unexpected page")

        self.flow.click.side_effect = click
        self.flow.navigate_to_cooperative_room_selection("home")
        self.assertEqual(self.flow.click.call_count, 3)
        self.assertEqual(self.flow.click.call_args_list[:2],
                         [call(self.retry_point), call(ca.HOME_LIVE_POINT)])
        self.assert_no_other_input()

    def test_delayed_modal_after_first_home_click_is_handled_before_reentry(self):
        current = self.screen(self.home)

        def click(point):
            if current[0] is self.home:
                current[0] = self.popup
            elif current[0] is self.popup:
                self.assertEqual(point, self.retry_point)
                current[0] = self.entry
            elif current[0] is self.entry:
                current[0] = self.room
            else:
                self.fail("clicked an unexpected page")

        self.flow.click.side_effect = click
        self.flow.navigate_to_cooperative_room_selection("home")
        self.assertEqual(self.flow.click.call_args_list[:2],
                         [call(ca.HOME_LIVE_POINT), call(self.retry_point)])
        self.assertEqual(self.flow.click.call_count, 3)
        self.assert_no_other_input()

    def test_persistent_modal_never_clicks_background_and_reports_network_error(self):
        self.screen(self.popup)
        with self.assertRaisesRegex(RuntimeError, "连接失败.*5次"):
            self.flow.navigate_to_cooperative_room_selection("home")
        self.assertEqual(self.flow.click.call_args_list, [call(self.retry_point)] * 5)
        self.assertLess(self.clock.now, 7.)
        self.assert_no_other_input()

    def test_modal_disappearance_and_reappearance_does_not_reset_budget(self):
        blank = np.zeros_like(self.popup)
        captures = [0]

        def capture():
            captures[0] += 1
            self.clock.advance(.51)
            return self.popup if captures[0] % 2 else blank

        self.screen(self.popup)
        self.flow.capture = capture
        with self.assertRaisesRegex(RuntimeError, "连接失败.*5次"):
            self.flow.navigate_to_cooperative_room_selection("home")
        self.assertEqual(self.flow.click.call_args_list, [call(self.retry_point)] * 5)
        self.assert_no_other_input()

    def test_result_navigation_retries_before_home_and_uses_no_back(self):
        current = self.screen(self.popup)
        self.flow.click.side_effect = lambda point: current.__setitem__(0, self.home)
        self.assertFalse(self.flow.navigate_completed_result(replay=False))
        self.flow.click.assert_called_once_with(self.retry_point)
        self.assert_no_other_input()

    def test_result_persistent_modal_has_same_bound_without_animation_input(self):
        self.screen(self.popup)
        with self.assertRaisesRegex(RuntimeError, "连接失败.*5次"):
            self.flow.navigate_completed_result(replay=True)
        self.assertEqual(self.flow.click.call_args_list, [call(self.retry_point)] * 5)
        self.assert_no_other_input()

    def test_post_score_destination_does_not_accept_home_behind_dialog(self):
        self.screen(self.popup)
        self.assertEqual(self.flow.wait_for_post_score_destination(
            ("room_search", "live_entry"), timeout=2.,
            connection_retry=ca.ConnectRetryState()), "story")
        self.flow.click.assert_called_once_with(self.retry_point)
        self.assert_no_other_input()

    def test_escape_dismiss_verifies_modal_disappearance(self):
        current = self.screen(self.popup)
        self.flow.click.side_effect = lambda point: current.__setitem__(0, self.home)
        self.assertTrue(self.flow.dismiss_connect_failed())
        self.flow.click.assert_called_once_with(self.retry_point)
        self.assert_no_other_input()

    def test_escape_dismiss_returns_failure_after_retry_limit(self):
        self.screen(self.popup)
        self.assertFalse(self.flow.dismiss_connect_failed())
        self.assertEqual(self.flow.click.call_args_list, [call(self.retry_point)] * 5)
        self.assert_no_other_input()

    def test_stop_before_helper_sends_no_input(self):
        self.context.tasker.stopping = True
        with self.assertRaises(InterruptedError):
            self.flow.handle_connect_failed(self.popup, ca.ConnectRetryState(), phase="test")
        self.flow.click.assert_not_called()
        self.assert_no_other_input()

    def test_stop_during_capture_does_not_retry_or_click_background(self):
        def capture_and_stop():
            self.context.tasker.stopping = True
            return self.popup

        self.flow.capture = capture_and_stop
        with self.assertRaises(InterruptedError):
            self.flow.navigate_to_cooperative_room_selection("home")
        self.flow.click.assert_not_called()
        self.assert_no_other_input()

    def test_recovery_preserves_specific_connection_failure_in_final_error(self):
        previous_reason = ca.latest_failure_reason()
        self.addCleanup(ca.record_failure_reason, previous_reason)
        specific_reason = "连接失败弹窗重试 5 次后仍未消失；本次任务停止"

        def fail_recovery(*_args):
            ca.record_failure_reason(specific_reason)
            return False

        self.flow.navigate_to_cooperative_room_selection = Mock()
        with patch.object(ca, "discard_prearmed_backend"), \
             patch.object(ca.CommonRecover, "run", side_effect=fail_recovery):
            with self.assertRaisesRegex(RuntimeError, "连接失败.*5 次") as error:
                self.flow.recover_after_play_failure("RealtimeProfilePlay 返回失败")
        self.assertIn(specific_reason, str(error.exception))
        self.assertNotIn("RealtimeProfilePlay 返回失败", str(error.exception))
        self.flow.navigate_to_cooperative_room_selection.assert_not_called()

    def test_navigation_screenshot_and_event_use_the_current_recording_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory)
            current_run = NS(recording_path=str(recording))

            def append(_root, phase, status, *, details):
                return append_lifecycle_event(recording, phase, status, details=details)

            with patch.object(ca, "current_live_run", return_value=current_run), \
                 patch.object(ca, "append_current_run_event", side_effect=append):
                ca.CooperativeLiveFlow._save_navigation_evidence(
                    self.flow, self.popup, "room-reentry", "connect-retry", attempt=1)
            records = (recording / "lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 1)
            event = json.loads(records[0])
            self.assertEqual((event["phase"], event["status"]),
                             ("room-reentry", "connect-retry"))
            self.assertEqual(event["details"]["attempt"], 1)
            screenshot = Path(event["details"]["screenshot"])
            self.assertFalse(screenshot.is_absolute())
            self.assertEqual(screenshot.parts[0], "navigation")
            image = imread_unicode(recording / screenshot)
            self.assertIsNotNone(image)
            np.testing.assert_array_equal(image, self.popup)


if __name__ == "__main__":
    unittest.main()
