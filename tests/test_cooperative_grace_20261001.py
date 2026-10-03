"""Replay background changes near first notes without device input."""

import unittest

import numpy as np

from test_team_live import ROOT
from realtime.native_play import NativeStartPhotogate
from realtime.vision_io import imread_unicode


class CooperativeBackgroundGraceTests(unittest.TestCase):
    def gate(self, mode="cooperative-playfield-confirmed", **kwargs):
        result = NativeStartPhotogate(
            mode=mode, stable_duration_ms=120,
            playfield_detector=kwargs.pop("playfield_detector", lambda image: True),
            popup_detector=kwargs.pop("popup_detector", lambda image: False),
            **kwargs,
        )
        result._startup_life = None
        return result

    def stable(self, gate, image, start, duration=.15):
        for stamp in np.arange(start, start + duration, 1 / 60):
            self.assertIsNone(gate.observe(image, float(stamp)))

    def first_note(self, background):
        # Preserve a recorded note rim and colour, with a controlled stage
        # backdrop so the replay isolates the incident's lighting transition.
        recorded = imread_unicode(
            ROOT / "tests/fixtures/cooperative-20260929/success-mania.png"
        )
        result = background.copy()
        result[455:550, 790:980] = recorded[455:550, 790:980]
        return result

    def background_change(self, gate, background, stamp):
        self.assertIsNone(gate.observe(background, stamp))
        self.assertEqual(
            gate.report()["photogate_events"][-1]["event"], "broad-change-blocked"
        )
        self.stable(gate, background, stamp + 1 / 60)
        return stamp + .15

    def test_mellow_first_note_is_not_hidden_by_a_second_grace_window(self):
        gate = self.gate()
        dark = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, dark, 5.31)
        bright = np.full_like(dark, 30)
        frozen = self.background_change(gate, bright, 8.794)
        self.assertLess(frozen, 9.0)
        self.stable(gate, bright, 8.96, duration=9.276 - 8.96)
        anchor = gate.observe(self.first_note(bright), 9.276)
        self.assertIsNotNone(anchor)
        self.assertGreater(anchor, 9.276 + .190 - 1 / 60)
        self.assertLessEqual(anchor, 9.276 + .190)

    def test_two_background_changes_do_not_accumulate_grace(self):
        gate = self.gate()
        dark = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, dark, 9.126)
        first = np.full_like(dark, 30)
        self.background_change(gate, first, 11.609)
        second = np.full_like(dark, 60)
        self.background_change(gate, second, 12.427)
        self.assertIsNotNone(gate.observe(self.first_note(second), 12.61))

    def test_initial_grace_still_rejects_an_early_note(self):
        gate = self.gate()
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, blank, 0.)
        self.assertIsNone(gate.observe(self.first_note(blank), .3))
        self.assertIsNone(gate.observe(blank, .4))
        self.assertIsNotNone(gate.observe(self.first_note(blank), .7))

    def test_real_popup_requires_a_fresh_grace_window(self):
        popup = [False]
        gate = self.gate(popup_detector=lambda image: popup[0])
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, blank, 0.)
        popup[0] = True
        self.assertIsNone(gate.observe(blank, 1.))
        popup[0] = False
        self.assertIsNone(gate.observe(blank, 1.1))
        self.stable(gate, blank, 1.12)
        self.assertIsNone(gate.observe(self.first_note(blank), 1.4))
        self.assertIsNone(gate.observe(blank, 1.5))
        self.assertIsNotNone(gate.observe(self.first_note(blank), 1.9))

    def test_playfield_loss_requires_a_fresh_grace_window(self):
        visible = [True]
        gate = self.gate(playfield_detector=lambda image: visible[0])
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, blank, 0.)
        visible[0] = False
        self.assertIsNone(gate.observe(blank, 1.))
        visible[0] = True
        self.stable(gate, blank, 1.1)
        self.assertIsNone(gate.observe(self.first_note(blank), 1.4))
        self.assertIsNone(gate.observe(blank, 1.5))
        self.assertIsNotNone(gate.observe(self.first_note(blank), 1.9))

    def test_team_background_policy_is_unchanged(self):
        gate = self.gate(mode="team-playfield-confirmed")
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, blank, 0.)
        changed = np.full_like(blank, 30)
        frozen = self.background_change(gate, changed, 1.)
        self.assertIsNone(gate.observe(self.first_note(changed), frozen + .03))

    def test_lighting_without_note_rims_cannot_start_after_background_reset(self):
        gate = self.gate()
        blank = np.zeros((720, 1280, 3), np.uint8)
        self.stable(gate, blank, 0.)
        changed = np.full_like(blank, 30)
        self.background_change(gate, changed, 1.)
        fixtures = ROOT / "tests/fixtures/cooperative-20260929"
        for index, name in enumerate(
            ("early-trigger.png", "early-00.png", "early-01.png", "early-02.png")
        ):
            self.assertIsNone(
                gate.observe(imread_unicode(fixtures / name), 1.3 + .2 * index),
                name,
            )
        self.assertFalse(gate.triggered)


if __name__ == "__main__":
    unittest.main()
