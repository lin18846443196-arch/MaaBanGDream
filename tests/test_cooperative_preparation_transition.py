"""准备页动画不能提前终止黑场监听；离线检查不向模拟器输入。"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.vision_io import imread_unicode


PRIVATE = ROOT / "tests/fixtures/cooperative-20261003"


def load(path):
    image = imread_unicode(path)
    if image is None:
        raise AssertionError(f"Missing recorded evidence: {path}")
    return image


def failure_frames():
    return [load(PRIVATE / f"20261003-222533-154678-{index:02d}-"
                 "playfield-motion-missed-transition.png") for index in range(8)]


class PreparationTransitionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = Clock()
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.flow = ca.CooperativeLiveFlow(self.context, dict(ca.DEFAULT_SETTINGS))
        self.flow.make_final_cover_entry_resolver = Mock(return_value=None)
        self.flow.wait_for_life_depleted_after_missed_transition = Mock(
            side_effect=ca.CooperativeStartupRetry("true missed transition"))
        self.flow._save_startup_failure_evidence = Mock()
        self.flow.play = Mock()
        self.flow.click = Mock()
        self.black = np.zeros((720, 1280, 3), np.uint8)

    def frames(self, images):
        sequence = iter(images)

        def capture():
            self.clock.advance(.01)
            return next(sequence)

        self.flow.capture_startup = Mock(side_effect=capture)

    def assert_no_input(self):
        self.flow.play.assert_not_called()
        self.flow.click.assert_not_called()
        self.context.tasker.controller.post_click.assert_not_called()
        self.context.tasker.controller.post_click_key.assert_not_called()

    def preparation_image(self, name="ready_cancel_button"):
        image = np.full_like(self.black, 60)
        template = self.flow.templates[name]
        x, y = ca.TEMPLATE_POSITIONS[name]
        image[y:y + template.shape[0], x:x + template.shape[1]] = template
        return image

    def test_ready_and_cancel_controls_block_positive_playfield_motion(self):
        for name in ("ready_button", "ready_cancel_button"):
            with self.subTest(control=name):
                preparation = self.preparation_image(name)
                self.frames([preparation] * 8 + [self.black])
                self.flow.playfield_detector = Mock(return_value=True)
                self.flow.playfield_entry_evidence.observe = Mock(return_value=True)
                self.flow.playfield_entry_evidence.reset = Mock()
                self.assertEqual(self.flow.watch_member_exit_before_black(), "black")
                self.assertEqual(self.flow.capture_startup.call_count, 9)
                self.flow.playfield_detector.assert_not_called()
                self.flow.playfield_entry_evidence.observe.assert_not_called()
                self.assertEqual(self.flow.playfield_entry_evidence.reset.call_count, 9)
                self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
                self.assert_no_input()

    def test_recorded_eight_false_positive_frames_wait_until_black(self):
        images = failure_frames()
        old_evidence = ca.CooperativePlayfieldEntryEvidence()
        self.assertEqual(
            [old_evidence.observe(image, playfield_visible=self.flow.playfield_detector(image))
             for image in images], [False] * 7 + [True])
        self.assertTrue(self.flow.preparation_controls_visible(load(PRIVATE / "preparation.png")))
        for image in images:
            self.assertTrue(self.flow.preparation_controls_visible(image))
        self.frames(images + [self.black])
        self.assertEqual(self.flow.watch_member_exit_before_black(), "black")
        self.assertEqual(self.flow.capture_startup.call_count, 9)
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.flow._save_startup_failure_evidence.assert_not_called()
        self.assert_no_input()

    def test_recorded_preparation_never_reaches_final_cover_resolver(self):
        images = failure_frames()
        cover = load(ROOT / "tests/fixtures/cooperative-20261002/dokusoushusa-cover.png")
        self.assertFalse(self.flow.preparation_controls_visible(cover))
        resolution = object()
        resolver = Mock()
        resolver.observe.side_effect = [None, resolution]
        self.flow.make_final_cover_entry_resolver.return_value = resolver
        self.frames(images + [cover, cover])
        with patch.object(ca, "update_live_run") as update:
            self.assertEqual(self.flow.watch_member_exit_before_black(), "final-cover")
        self.assertEqual(resolver.observe.call_count, 2)
        self.assertIs(update.call_args.kwargs["startup_final_cover_resolution"], resolution)
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()

    def test_preparation_resets_a_prior_note_motion_streak(self):
        base = np.full_like(self.black, 60)
        notes = []
        for value in (60, 150, 240):
            frame = base.copy()
            frame[430:570, :30] = value
            notes.append(frame)
        self.flow.playfield_detector = Mock(return_value=True)
        self.flow.playfield_entry_evidence._note_head_detector = Mock(return_value=True)
        self.flow.playfield_entry_evidence._prepare_popup_detector = Mock(return_value=False)
        self.frames(notes[:2] + [self.preparation_image()] + notes[2:] + [self.black])
        self.assertEqual(self.flow.watch_member_exit_before_black(), "black")
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()

    def test_return_to_preparation_discards_partial_cover_confirmation(self):
        cover = np.full_like(self.black, 60)
        first, second = Mock(), Mock()
        first.observe.return_value = None
        resolution = object()
        second.observe.side_effect = [None, resolution]
        self.flow.make_final_cover_entry_resolver.side_effect = [first, second]
        self.frames([cover, self.preparation_image(), cover, cover])
        with patch.object(ca, "update_live_run"):
            self.assertEqual(self.flow.watch_member_exit_before_black(), "final-cover")
        self.assertEqual(first.observe.call_count, 1)
        self.assertEqual(second.observe.call_count, 2)
        self.assert_no_input()

    def test_member_exit_has_priority_over_preparation_guard(self):
        self.frames([self.preparation_image()])
        self.flow.visible = Mock(return_value=True)
        self.flow.dismiss_member_exit = Mock()
        with self.assertRaises(ca.MemberExited):
            self.flow.watch_member_exit_before_black()
        self.flow.dismiss_member_exit.assert_called_once()
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()

    def test_preparation_wait_keeps_the_existing_timeout(self):
        self.flow.capture_startup = Mock(return_value=self.preparation_image())
        self.flow.jump_after_download_timeout = Mock(
            side_effect=ca.CooperativeStartupRetry("bounded preparation wait"))
        with self.assertRaises(ca.CooperativeStartupRetry):
            self.flow.watch_member_exit_before_black(timeout=.1)
        self.assertLess(self.clock.now, .13)
        self.flow.jump_after_download_timeout.assert_called_once()
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assertEqual(self.flow._save_startup_failure_evidence.call_args.args[1],
                         "transition-timeout")
        self.assert_no_input()

    def test_user_stop_while_waiting_on_preparation_sends_no_recovery(self):
        def capture():
            self.context.tasker.stopping = True
            return self.preparation_image()

        self.flow.capture_startup = Mock(side_effect=capture)
        self.flow.jump_after_download_timeout = Mock()
        with self.assertRaises(InterruptedError):
            self.flow.watch_member_exit_before_black()
        self.assertEqual(self.flow.capture_startup.call_count, 1)
        self.flow.jump_after_download_timeout.assert_not_called()
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
