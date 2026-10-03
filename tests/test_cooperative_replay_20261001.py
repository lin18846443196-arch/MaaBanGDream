"""Replay the recorded cooperative settlement without emulator input."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, call, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.vision_io import imread_unicode


FIXTURES = ROOT / "tests/fixtures/cooperative-replay-20261001"


class CooperativeReplayTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = Clock()
        self.controller = Mock()
        self.context = NS(tasker=NS(stopping=False, controller=self.controller))
        self.settings = dict(ca.DEFAULT_SETTINGS)
        self.flow = ca.CooperativeLiveFlow(self.context, self.settings)
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        patch.object(ca, "require_game_foreground").start()
        self.back = patch.object(ca, "accelerated_back").start()
        patch.object(ca, "append_current_run_event").start()
        self.activity = self.load(FIXTURES / "activity-points.png")
        self.room = self.load(FIXTURES / "room-selection.png")
        self.home = self.load(ROOT / "tests/fixtures/team/home-circle.png")
        self.flow.pipeline_box = Mock(return_value=None)
        self.flow.click = Mock()

    def load(self, path):
        image = imread_unicode(path)
        self.assertIsNotNone(image, str(path))
        return image

    def current_screen(self, image):
        current = [image]

        def capture():
            if self.flow.stopped():
                raise InterruptedError("用户已停止任务")
            self.clock.advance(.01)
            return current[0]

        self.flow.capture = capture
        self.flow.pipeline_box = Mock(side_effect=lambda screen, node:
            NS(x=1, y=1, w=1, h=1)
            if screen is self.home and node == "CooperativeHomeMarker" else None)
        return current

    def assert_button_click(self, point, key):
        box = self.flow.result_buttons(self.activity)[key]
        x, y, width, height = box
        self.assertTrue(x <= point[0] < x + width, point)
        self.assertTrue(y <= point[1] < y + height, point)

    def mock_rounds(self, count, outcomes):
        self.settings.update(count=count, disconnect_jump_enabled=True)
        self.flow.run_attempt = Mock(side_effect=outcomes)
        self.flow.recover_after_play_failure = Mock()
        self.flow.progress_callback = Mock()
        self.flow.navigate_completed_result = Mock(return_value=False)

    def test_recorded_final_page_has_both_real_footer_buttons(self):
        buttons = self.flow.result_buttons(self.activity)
        self.assertIsNotNone(buttons)
        self.assertEqual(set(buttons), {"replay", "confirm"})
        for key, expected_x in (("replay", 707), ("confirm", 959)):
            x, y, width, height = buttons[key]
            self.assertLessEqual(abs(x - expected_x), 1)
            self.assertLessEqual(abs(y - 618), 1)
            self.assertGreater(width, 0)
            self.assertGreater(height, 0)

    def test_room_home_playfield_and_popup_are_not_result_buttons(self):
        for path in (
            FIXTURES / "room-selection.png",
            ROOT / "tests/fixtures/team/home-circle.png",
            ROOT / "tests/fixtures/cooperative-20260929/first-real-notes.png",
            ROOT / "tests/fixtures/cooperative-20260930/restriction-24s.png",
        ):
            with self.subTest(page=path.name):
                self.assertIsNone(self.flow.result_buttons(self.load(path)))

    def test_both_buttons_must_be_present_in_the_same_frame(self):
        for left in (707, 959):
            with self.subTest(missing_button_x=left):
                image = self.activity.copy()
                image[610:685, left - 10:left + 230] = 100
                self.assertIsNone(self.flow.result_buttons(image))

    def test_modal_shade_does_not_allow_clicking_background_buttons(self):
        image = (self.activity.astype(np.float32) * .5).astype(np.uint8)
        image[210:525, 390:890] = 240
        self.assertIsNone(self.flow.result_buttons(image))

    def test_recorded_replay_returns_room_selection_without_back_or_room_reuse(self):
        current = self.current_screen(self.activity)
        self.flow.click.side_effect = lambda _point: current.__setitem__(0, self.room)
        self.assertFalse(self.flow.navigate_completed_result(replay=True))
        self.flow.click.assert_called_once()
        self.assert_button_click(self.flow.click.call_args.args[0], "replay")
        self.back.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_last_round_confirm_reaches_home_without_replay_or_back(self):
        current = self.current_screen(self.activity)
        self.flow.click.side_effect = lambda _point: current.__setitem__(0, self.home)
        self.assertFalse(self.flow.navigate_completed_result(replay=False))
        self.flow.click.assert_called_once()
        self.assert_button_click(self.flow.click.call_args.args[0], "confirm")
        self.back.assert_not_called()

    def test_activity_page_after_animation_skip_is_inspected_before_pending_back(self):
        current = self.current_screen(np.zeros_like(self.activity))

        def skip_animation(*_point):
            current[0] = self.activity
            return Mock()

        self.controller.post_click.side_effect = skip_animation
        self.flow.click.side_effect = lambda _point: current.__setitem__(0, self.room)
        self.assertFalse(self.flow.navigate_completed_result(replay=True))
        self.controller.post_click.assert_called_once_with(1279, 719)
        self.controller.post_click_key.assert_not_called()
        self.flow.click.assert_called_once()
        self.assert_button_click(self.flow.click.call_args.args[0], "replay")

    def test_loading_after_replay_gets_no_back_or_animation_input(self):
        clicked_at = [None]

        def capture():
            self.clock.advance(.01)
            if clicked_at[0] is None:
                return self.activity
            if self.clock.now - clicked_at[0] < 1.0:
                return np.zeros_like(self.activity)
            return self.room

        self.flow.capture = capture
        self.flow.click.side_effect = lambda _point: clicked_at.__setitem__(0, self.clock.now)
        self.assertFalse(self.flow.navigate_completed_result(replay=True))
        self.flow.click.assert_called_once()
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()
        self.back.assert_not_called()

    def test_replay_loading_timeout_is_bounded_and_does_not_cancel_reentry(self):
        current = self.current_screen(self.activity)
        self.flow.click.side_effect = lambda _point: current.__setitem__(
            0, np.zeros_like(self.activity))
        with self.assertRaisesRegex(RuntimeError, "60秒"):
            self.flow.navigate_completed_result(replay=True)
        self.assertGreaterEqual(self.clock.now, 60.)
        self.assertLess(self.clock.now, 61.)
        self.flow.click.assert_called_once()
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()
        self.back.assert_not_called()

    def test_dropped_footer_clicks_retry_three_times_then_fail_without_back(self):
        self.current_screen(self.activity)
        with self.assertRaisesRegex(RuntimeError, "3次"):
            self.flow.navigate_completed_result(replay=True)
        self.assertEqual(self.flow.click.call_count, 3)
        self.assertLess(self.clock.now, 5.)
        for footer_call in self.flow.click.call_args_list:
            self.assert_button_click(footer_call.args[0], "replay")
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_one_round_only_confirms(self):
        self.mock_rounds(1, [True])
        self.assertTrue(self.flow.run())
        self.flow.navigate_completed_result.assert_called_once_with(replay=False)
        self.flow.progress_callback.assert_called_once_with(1, 1)

    def test_two_rounds_replay_once_then_confirm(self):
        self.mock_rounds(2, [True, True])
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.navigate_completed_result.call_args_list,
                         [call(replay=True), call(replay=False)])
        self.assertEqual(self.flow.run_attempt.call_args_list,
                         [call(reuse_room=False), call(reuse_room=False)])
        self.assertEqual(self.flow.progress_callback.call_args_list,
                         [call(1, 2), call(2, 2)])

    def test_infinite_rounds_replay_until_stopped(self):
        self.mock_rounds(0, [True, True])
        navigation_count = [0]

        def navigate(*, replay):
            navigation_count[0] += 1
            if navigation_count[0] == 2:
                self.context.tasker.stopping = True
            return False

        self.flow.navigate_completed_result.side_effect = navigate
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.navigate_completed_result.call_args_list,
                         [call(replay=True), call(replay=True)])
        self.assertEqual(self.flow.run_attempt.call_count, 2)
        self.assertEqual(self.flow.progress_callback.call_args_list,
                         [call(1, 0), call(2, 0)])

    def test_failed_attempts_do_not_consume_rounds_or_select_confirm_early(self):
        self.mock_rounds(2, [False, True, False, True])
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_count, 4)
        self.assertEqual(self.flow.recover_after_play_failure.call_count, 2)
        self.assertEqual(self.flow.navigate_completed_result.call_args_list,
                         [call(replay=True), call(replay=False)])
        self.assertEqual(self.flow.progress_callback.call_args_list,
                         [call(1, 2), call(2, 2)])

    def test_cancelled_result_navigation_sends_no_input(self):
        self.context.tasker.stopping = True
        self.flow.capture = Mock(side_effect=AssertionError("capture after stop"))
        try:
            self.flow.navigate_completed_result(replay=True)
        except InterruptedError:
            pass
        self.flow.capture.assert_not_called()
        self.flow.click.assert_not_called()
        self.back.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_stop_during_capture_does_not_click_the_returned_footer(self):
        def capture_and_stop():
            self.context.tasker.stopping = True
            return self.activity

        self.flow.capture = Mock(side_effect=capture_and_stop)
        with self.assertRaises(InterruptedError):
            self.flow.navigate_completed_result(replay=True)
        self.flow.click.assert_not_called()
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_stop_after_play_does_not_start_settlement_navigation(self):
        self.mock_rounds(1, [True])

        def play_and_stop(**_kwargs):
            self.context.tasker.stopping = True
            return True

        self.flow.run_attempt.side_effect = play_and_stop
        self.assertTrue(self.flow.run())
        self.flow.navigate_completed_result.assert_not_called()

    def test_joined_room_stay_popup_reuses_room_only_for_middle_rounds(self):
        for method in ("friend", "private"):
            with self.subTest(method=method):
                self.settings.update(entry_method=method, post_live_action="stay")
                popup = np.full_like(self.activity, 100)
                template = self.flow.templates["repeat_room_title"]
                x, y = ca.TEMPLATE_POSITIONS["repeat_room_title"]
                popup[y:y + template.shape[0], x:x + template.shape[1]] = template
                self.current_screen(popup)
                self.flow.stay_in_room = Mock()
                self.assertTrue(self.flow.navigate_completed_result(replay=True))
                self.flow.stay_in_room.assert_called_once()
                self.flow.click.assert_not_called()
                self.back.assert_not_called()

    def test_final_round_confirms_even_when_joined_room_stay_is_selected(self):
        for method in ("friend", "private"):
            with self.subTest(method=method):
                self.settings.update(entry_method=method, post_live_action="stay")
                self.mock_rounds(1, [True])
                self.flow.stay_in_room = Mock()
                self.assertTrue(self.flow.run())
                self.flow.navigate_completed_result.assert_called_once_with(replay=False)
                self.flow.stay_in_room.assert_not_called()

    def test_last_joined_round_declines_repeat_prompt_after_confirm(self):
        self.settings.update(entry_method="private", post_live_action="stay")
        popup = np.full_like(self.activity, 100)
        template = self.flow.templates["repeat_room_title"]
        x, y = ca.TEMPLATE_POSITIONS["repeat_room_title"]
        popup[y:y + template.shape[0], x:x + template.shape[1]] = template
        current = self.current_screen(self.activity)
        self.flow.stay_in_room = Mock()

        def advance(_point):
            current[0] = popup if current[0] is self.activity else self.home

        self.flow.click.side_effect = advance
        self.assertFalse(self.flow.navigate_completed_result(replay=False))
        self.assertEqual(self.flow.click.call_count, 2)
        self.assert_button_click(self.flow.click.call_args_list[0].args[0], "confirm")
        self.assertEqual(self.flow.click.call_args_list[1], call((512, 447)))
        self.flow.stay_in_room.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_verified_room_reuse_is_passed_to_next_attempt(self):
        self.settings.update(entry_method="private", post_live_action="stay")
        self.mock_rounds(2, [True, True])
        self.flow.navigate_completed_result.side_effect = [True, False]
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_args_list,
                         [call(reuse_room=False), call(reuse_room=True)])

    def test_finalize_returns_home_with_stay_option_selected(self):
        self.settings.update(entry_method="private", post_live_action="stay")
        argv = NS(custom_action_param="{}")
        with patch.object(ca, "current_cooperative_settings", return_value=self.settings), \
             patch.object(ca.CommonRecover, "run", return_value=True) as recover:
            self.assertTrue(ca.CooperativeLiveFinalize().run(self.context, argv))
        recover.assert_called_once_with(self.context, argv)


if __name__ == "__main__":
    unittest.main()
