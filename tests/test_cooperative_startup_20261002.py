"""Actual note evidence for startup recovery; all checks run without a device."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import cv2
import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime import native_play as npy
from realtime.first_note_evidence import has_approaching_note_head
from realtime.prepare_popup import CooperativePreparePopupDetector
from realtime.vision_io import imread_unicode


FIXTURES = ROOT / "tests/fixtures/cooperative-20260929"
WAITING_IMAGE = ROOT / "tests/fixtures/cooperative-20261002/waiting-members.png"


def load(path):
    image = imread_unicode(path)
    if image is None:
        raise AssertionError(f"Missing recorded evidence: {path}")
    return image


def old_column_results(images):
    """Reproduce the removed motion-only decision for false-trigger evidence."""
    previous = None
    streak = 0
    results = []
    for image in images:
        columns = image[430:570, :, :3].astype("float32").mean(axis=(0, 2))
        if previous is None:
            results.append(False)
        else:
            changed = np.count_nonzero(np.abs(columns - previous) >= 18.)
            streak = 0 if changed < 12 or changed >= image.shape[1] * .30 else streak + 1
            results.append(streak >= 2)
        previous = columns
    return results


def narrow_flashes(base=None):
    if base is None:
        base = np.zeros((720, 1280, 3), np.uint8)
    images = []
    for value in (0, 120, 240):
        image = base.copy()
        image[430:570, 0:30] = value
        images.append(image)
    return images


class NoteHeadParityTests(unittest.TestCase):
    def test_all_recorded_note_and_light_outputs_preserved_at_each_resolution(self):
        gate = npy.NativeStartPhotogate(mode="cooperative-playfield-confirmed")
        for name, expected in (
            ("first-real-notes", True), ("success-mania", True),
            ("success-queen", True), ("success-sakura", True),
            ("early-trigger", False), ("early-00", False),
            ("early-01", False), ("early-02", False),
        ):
            for scale in (.75, 1., 1.5):
                with self.subTest(image=name, scale=scale):
                    image = cv2.resize(load(FIXTURES / f"{name}.png"), None,
                                       fx=scale, fy=scale)
                    self.assertEqual(has_approaching_note_head(image), expected)
                    self.assertEqual(gate._has_approaching_note_head(image), expected)

    def test_native_delegation_preserves_nondefault_band_arguments(self):
        gate = npy.NativeStartPhotogate(from_row=470, to_row=495, reference_height=600)
        image = np.zeros((600, 1000, 3), np.uint8)
        with patch.object(npy, "has_approaching_note_head", return_value=True) as detector:
            self.assertTrue(gate._has_approaching_note_head(image))
        detector.assert_called_once_with(image, from_row=470, to_row=495,
                                         reference_height=600)


class CooperativeEntryNoteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.evidence = ca.CooperativePlayfieldEntryEvidence()

    def test_narrow_flashes_that_passed_old_motion_guard_are_refused(self):
        images = narrow_flashes()
        self.assertEqual(old_column_results(images), [False, False, True])
        self.assertEqual([self.evidence.observe(image, playfield_visible=True)
                          for image in images], [False, False, False])
        self.assertTrue(all(not has_approaching_note_head(image) for image in images))

    def test_prior_real_lighting_sequence_never_counts_as_a_note(self):
        images = [load(FIXTURES / f"{name}.png") for name in
                  ("early-00", "early-01", "early-02", "early-trigger")]
        for image in images * 2:
            self.assertFalse(self.evidence.observe(image, playfield_visible=True))

    def test_recorded_success_sequence_confirms_actual_moving_notes(self):
        images = [load(FIXTURES / f"success-{name}.png")
                  for name in ("sakura", "queen", "mania")]
        self.assertEqual([self.evidence.observe(image, playfield_visible=True)
                          for image in images], [False, False, True])

    def test_static_first_notes_do_not_pass_without_motion(self):
        for name in ("first-real-notes", "success-mania", "success-queen", "success-sakura"):
            self.evidence.reset()
            image = load(FIXTURES / f"{name}.png")
            with self.subTest(image=name):
                self.assertTrue(has_approaching_note_head(image))
                for _ in range(5):
                    self.assertFalse(self.evidence.observe(image, playfield_visible=True))

    def test_first_real_notes_can_pass_with_the_retained_narrow_motion_evidence(self):
        images = narrow_flashes(load(FIXTURES / "first-real-notes.png"))
        self.assertEqual([self.evidence.observe(image, playfield_visible=True)
                          for image in images], [False, False, True])

    def test_actual_waiting_members_popup_blocks_even_old_qualifying_motion(self):
        popup = load(WAITING_IMAGE)
        self.assertTrue(CooperativePreparePopupDetector(verify_content=True)(popup))
        images = narrow_flashes(popup)
        self.assertEqual(old_column_results(images), [False, False, True])
        self.assertEqual([self.evidence.observe(image, playfield_visible=True)
                          for image in images], [False, False, False])

    def test_popup_is_checked_before_even_a_positive_note_guard(self):
        popup = load(WAITING_IMAGE)
        self.evidence._note_head_detector = Mock(return_value=True)
        for image in narrow_flashes(popup):
            self.assertFalse(self.evidence.observe(image, playfield_visible=True))
        self.evidence._note_head_detector.assert_not_called()

    def test_other_recorded_waiting_popup_is_also_blocked(self):
        popup = load(ROOT / "tests/fixtures/cooperative-20260930/waiting-members.png")
        for image in narrow_flashes(popup):
            self.assertFalse(self.evidence.observe(image, playfield_visible=True))

    def test_missing_note_clears_preceding_narrow_motion_streak(self):
        notes = narrow_flashes(load(FIXTURES / "first-real-notes.png"))
        self.assertFalse(self.evidence.observe(notes[0], playfield_visible=True))
        self.assertFalse(self.evidence.observe(notes[1], playfield_visible=True))
        self.assertFalse(self.evidence.observe(narrow_flashes()[2], playfield_visible=True))
        self.assertFalse(self.evidence.observe(notes[2], playfield_visible=True))

    def test_loss_of_playfield_and_explicit_reset_clear_previous_note_motion(self):
        notes = narrow_flashes(load(FIXTURES / "first-real-notes.png"))
        for reset in (lambda: self.evidence.reset(),
                      lambda: self.evidence.observe(notes[1], playfield_visible=False)):
            self.evidence.reset()
            self.assertFalse(self.evidence.observe(notes[0], playfield_visible=True))
            self.assertFalse(self.evidence.observe(notes[1], playfield_visible=True))
            reset()
            self.assertFalse(self.evidence.observe(notes[2], playfield_visible=True))

    def test_invalid_image_is_refused_without_opencv_errors(self):
        for image in (None, np.zeros((720, 1280), np.uint8),
                      np.zeros((0, 1280, 3), np.uint8),
                      np.zeros((720, 1, 3), np.uint8)):
            self.assertFalse(self.evidence.observe(image, playfield_visible=True))


class StartupWatcherNoteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.clock = Clock()
        patch.object(ca.time, "monotonic", self.clock.time).start()
        patch.object(ca.time, "sleep", self.clock.advance).start()
        self.controller = Mock()
        self.context = NS(tasker=NS(stopping=False, controller=self.controller))
        self.flow = ca.CooperativeLiveFlow(self.context, dict(ca.DEFAULT_SETTINGS))
        self.flow.visible = Mock(return_value=False)
        self.flow.playfield_detector = Mock(return_value=True)
        self.flow.make_final_cover_entry_resolver = Mock(return_value=None)
        self.flow.play = Mock()
        self.flow.click = Mock()
        self.flow.capture = Mock(side_effect=AssertionError("slow startup capture"))

    def frames(self, images):
        iterator = iter(images)

        def capture():
            self.clock.advance(.01)
            return next(iterator)

        self.flow.capture_startup = Mock(side_effect=capture)

    def assert_no_input(self):
        self.flow.play.assert_not_called()
        self.flow.click.assert_not_called()
        self.controller.post_click.assert_not_called()
        self.controller.post_click_key.assert_not_called()

    def test_lighting_motion_keeps_watching_until_black_transition(self):
        # Avoid the first all-zero flash being classified as a black transition.
        images = narrow_flashes(np.full((720, 1280, 3), 60, np.uint8))
        self.assertEqual(old_column_results(images), [False, False, True])
        self.frames(images + [np.zeros((720, 1280, 3), np.uint8)])
        self.flow.wait_for_life_depleted_after_missed_transition = Mock()
        self.assertEqual(self.flow.watch_member_exit_before_black(), "black")
        self.assertEqual(self.flow.capture_startup.call_count, 4)
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()

    def test_prior_lighting_sequence_keeps_watching_until_confirmed_final_cover(self):
        images = [load(FIXTURES / f"{name}.png") for name in
                  ("early-00", "early-01", "early-02", "early-trigger")]
        cover = np.full_like(images[0], 60)
        self.frames(images + [cover])
        resolution = object()
        resolver = Mock()
        resolver.observe.side_effect = [None] * len(images) + [resolution]
        self.flow.make_final_cover_entry_resolver.return_value = resolver
        self.flow.wait_for_life_depleted_after_missed_transition = Mock()
        with patch.object(ca, "update_live_run") as update:
            self.assertEqual(self.flow.watch_member_exit_before_black(), "final-cover")
        self.assertIs(update.call_args.kwargs["startup_final_cover_resolution"], resolution)
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()

    def test_actual_waiting_popup_keeps_watching_without_life_monitor_fallback(self):
        self.frames(narrow_flashes(load(WAITING_IMAGE)) +
                    [np.zeros((720, 1280, 3), np.uint8)])
        self.flow.wait_for_life_depleted_after_missed_transition = Mock()
        self.assertEqual(self.flow.watch_member_exit_before_black(), "black")
        self.flow.wait_for_life_depleted_after_missed_transition.assert_not_called()
        self.assert_no_input()

    def test_genuine_moving_notes_still_use_fail_closed_life_monitor(self):
        self.frames([load(FIXTURES / f"success-{name}.png")
                     for name in ("sakura", "queen", "mania")])
        self.flow.wait_for_life_depleted_after_missed_transition = Mock(
            side_effect=ca.CooperativeStartupRetry("missed transition safely recovered"))
        with self.assertRaises(ca.CooperativeStartupRetry):
            self.flow.watch_member_exit_before_black()
        self.flow.wait_for_life_depleted_after_missed_transition.assert_called_once()
        self.assert_no_input()

    def test_stop_after_a_genuine_note_capture_sends_no_recovery_input(self):
        images = [load(FIXTURES / f"success-{name}.png")
                  for name in ("sakura", "queen", "mania")]
        captures = [0]

        def capture():
            image = images[captures[0]]
            captures[0] += 1
            if captures[0] == 3:
                self.context.tasker.stopping = True
            return image

        self.flow.capture_startup = Mock(side_effect=capture)
        with self.assertRaises(InterruptedError):
            self.flow.watch_member_exit_before_black()
        self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
