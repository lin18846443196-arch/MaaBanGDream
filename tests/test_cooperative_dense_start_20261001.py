"""Dense first notes following stage lighting, without device input."""

import unittest

import cv2
import numpy as np

from test_team_live import ROOT
from realtime.native_play import NativeStartPhotogate
from realtime.vision_io import imread_unicode


class DenseCooperativeStartTests(unittest.TestCase):
    def gate(self, mode="cooperative-playfield-confirmed", **kwargs):
        result = NativeStartPhotogate(
            mode=mode, stable_duration_ms=120,
            playfield_detector=kwargs.pop("playfield_detector", lambda image: True),
            popup_detector=kwargs.pop("popup_detector", lambda image: False),
            **kwargs,
        )
        result._startup_life = None
        return result

    def prime(self, gate):
        image = np.zeros((720, 1280, 3), np.uint8)
        for stamp in np.arange(0., .9, 1 / 60):
            self.assertIsNone(gate.observe(image, float(stamp)))
        self.assertTrue(gate.frozen)
        return image

    def note(self, shade, vertical_shift):
        recorded = imread_unicode(
            ROOT / "tests/fixtures/cooperative-20260929/success-mania.png"
        )
        image = np.full_like(recorded, shade)
        # Move a real note image down at approximately the observed speed.
        top = 455 + vertical_shift
        image[top:top + 95, 790:980] = recorded[455:550, 790:980]
        return image

    def dense_opening(self, gate):
        broad = np.full((720, 1280, 3), 30, np.uint8)
        self.assertIsNone(gate.observe(broad, 1.))
        for index in range(1, 14):
            stamp = 1. + index / 60
            image = self.note(30 + 6 * (index % 2), -80 + 12 * index)
            anchor = gate.observe(image, stamp)
            if anchor is not None:
                return stamp, anchor
        return None

    def test_dense_moving_notes_trigger_without_a_new_quiet_window(self):
        gate = self.gate()
        self.prime(gate)
        result = self.dense_opening(gate)
        self.assertIsNotNone(result)
        stamp, anchor = result
        self.assertLessEqual(stamp, 1.15)
        self.assertAlmostEqual(anchor, stamp + .190)
        self.assertEqual(
            sum(e["event"] == "stable" for e in gate.report()["photogate_events"]), 1
        )

    def test_stage_lighting_is_rebased_without_becoming_a_note(self):
        gate = self.gate()
        self.prime(gate)
        for index in range(30):
            image = np.full((720, 1280, 3), 30 + 6 * (index % 2), np.uint8)
            cv2.rectangle(image, (635, 445), (645, 555), (255, 255, 255), -1)
            self.assertIsNone(gate.observe(image, 1. + index / 60))
        self.assertTrue(gate.frozen)
        self.assertFalse(gate.triggered)
        self.assertTrue(any(e["event"] == "note-head-missing"
                            for e in gate.report()["photogate_events"]))

    def test_real_popup_still_clears_the_validated_baseline(self):
        popup = [False]
        gate = self.gate(popup_detector=lambda image: popup[0])
        blank = self.prime(gate)
        self.assertIsNone(gate.observe(np.full_like(blank, 30), 1.))
        popup[0] = True
        self.assertIsNone(gate.observe(blank, 1.1))
        self.assertFalse(gate.frozen)
        popup[0] = False
        self.assertIsNone(gate.observe(blank, 1.2))
        for stamp in np.arange(1.22, 1.37, 1 / 60):
            self.assertIsNone(gate.observe(blank, float(stamp)))
        self.assertIsNone(gate.observe(self.note(0, 0), 1.4))

    def test_playfield_loss_still_clears_the_validated_baseline(self):
        visible = [True]
        gate = self.gate(playfield_detector=lambda image: visible[0])
        blank = self.prime(gate)
        self.assertIsNone(gate.observe(np.full_like(blank, 30), 1.))
        visible[0] = False
        self.assertIsNone(gate.observe(blank, 1.1))
        self.assertFalse(gate.frozen)
        visible[0] = True
        for stamp in np.arange(1.2, 1.35, 1 / 60):
            self.assertIsNone(gate.observe(blank, float(stamp)))
        self.assertIsNone(gate.observe(self.note(0, 0), 1.4))

    def test_team_still_requires_a_new_stable_baseline(self):
        gate = self.gate(mode="team-playfield-confirmed")
        self.prime(gate)
        self.assertIsNone(self.dense_opening(gate))
        self.assertFalse(gate.frozen)

    def test_prestart_life_protection_still_rejects_a_late_anchor(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from realtime.life_monitor import LifeReading

        gate = self.gate()
        blank = self.prime(gate)
        self.assertIsNone(gate.observe(np.full_like(blank, 30), 1.))
        gate._startup_life = SimpleNamespace(detect=Mock(side_effect=[
            LifeReading(True, value) for value in (1000, 900, 900, 900)
        ]))
        for stamp in (1.1, 1.21, 1.32):
            self.assertIsNone(gate.observe(blank, stamp))
        with self.assertRaisesRegex(RuntimeError, "拒绝从歌曲中段启动"):
            gate.observe(self.note(0, 0), 1.43)
        self.assertFalse(gate.triggered)


if __name__ == "__main__":
    unittest.main()
