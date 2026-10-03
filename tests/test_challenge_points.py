"""Offline point selection against the October 1 recording; no device input."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT
from realtime import challenge_points as cp
from realtime import difficulty_action as difficulty
from realtime import preparation_identity as identity
from realtime.live_session import current_live_run
from realtime.vision_io import imread_unicode
import task_reporting as reporting


FIXTURES = ROOT / "tests" / "fixtures" / "challenge"


def frame(name):
    image = imread_unicode(FIXTURES / f"{name}.png")
    assert image is not None, name
    return image


class ReadingTests(unittest.TestCase):
    def test_recorded_balances_and_radio_states(self):
        for name, multiplier in (("points-one", 1), ("points-eight", 8)):
            with self.subTest(name=name):
                image = frame(name)
                self.assertEqual(cp.read_points(image), 15830)
                self.assertEqual(cp.selected_multiplier(image), multiplier)

    def test_cost_boundaries(self):
        cases = ((0, None), (199, None), (200, 1), (399, 1),
                 (400, 2), (799, 2), (800, 4), (1599, 4), (1600, 8), (15830, 8))
        for points, expected in cases:
            with self.subTest(points=points):
                self.assertEqual(cp.affordable_multiplier(points), expected)

    def test_lone_zero_requires_shape_confirmation(self):
        image = frame("points-one")
        x, y, width, height = cp.POINTS_ROI
        # Retain the recording's rightmost zero and erase preceding digits.
        image[y:y + height, x:819] = 255
        self.assertEqual(cp.read_points(image), 0)
        with patch.object(cp, "_digit_shape_scores", return_value=[(.2, 6), (.3, 0)]):
            self.assertIsNone(cp.read_points(image))

    def test_strict_numeric_parser_and_bundled_dictionary(self):
        for text, value in (("15830", 15830), ("15,830", 15830), ("０", 0),
                            ("'1''5''8''3''0'", 15830), ("200", 200)):
            self.assertEqual(cp.parse_points(text), value)
        for text in ("", "CP200", "-200", "200.0", "1,2", "15830/99999", "2O0", "1 600"):
            self.assertIsNone(cp.parse_points(text), text)

    def test_ocr_uncertainty_is_not_a_balance(self):
        with patch.object(cp, "recognize_song_title", return_value=NS(text="1600", confidence=.89)):
            self.assertIsNone(cp.read_points(frame("points-one")))
        self.assertIsNone(cp.read_points(np.zeros((720, 1280, 3), dtype=np.uint8)))

    def test_recorded_song_and_preparation_identity(self):
        image = frame("song-select")
        self.assertEqual(difficulty.selected_difficulty(image), "Expert")
        self.assertEqual(difficulty.read_song_level(image), 26)
        title = identity.read_preparation_title(frame("band-ready"))
        self.assertIsNotNone(title)
        self.assertIn("MATSURI", title.text)

    def test_recorded_entry_resolves_the_same_chart_on_preparation(self):
        nodes = json.loads((ROOT / "resource/pipeline/challenge_live.json").read_text(encoding="utf-8"))
        params = dict(nodes["ChallengeDifficulty"]["custom_action_param"], difficulty="Expert")
        context = NS(tasker=NS(stopping=False, controller=Mock()),
                     run_recognition=Mock(return_value=NS(hit=True)))
        with patch.object(difficulty, "capture_image", return_value=frame("song-select")), \
                patch.object(difficulty, "require_game_foreground"), patch.object(difficulty.time, "sleep"):
            self.assertTrue(difficulty.RealtimeDifficultySelect().run(
                context, NS(custom_action_param=json.dumps(params))))
        before = current_live_run()
        after = identity.confirm_preparation_identity(frame("band-ready"), "Expert")
        self.assertEqual(before.run_id, after.run_id)
        self.assertEqual(after.song_level, 26)
        self.assertTrue(after.prepared_for_play)
        self.assertFalse(after.preparation_identity_pending_final_cover)
        resolution = difficulty.resolve_chart_for_selected_song(
            after.song_id, after.difficulty, after.song_level, after.song_title)
        self.assertIsNotNone(resolution.selection)
        self.assertEqual(resolution.selection.bestdori_song_id, 774)


class PointActionTests(unittest.TestCase):
    def setUp(self):
        self.image = frame("points-one")
        self.context = NS(tasker=NS(stopping=False, controller=Mock()),
                          run_recognition=Mock(return_value=NS(hit=True)),
                          override_next=Mock(return_value=True))
        self.args = NS(node_name="ChallengePointSelect", custom_action_param="{}")
        self.capture = self.enterContext(patch.object(cp, "capture_image", return_value=self.image))
        self.read = self.enterContext(patch.object(cp, "read_points", return_value=15830))
        self.click = self.enterContext(patch.object(cp, "click"))
        self.enterContext(patch.object(cp, "wait"))
        self.evidence = self.enterContext(patch.object(cp, "evidence"))

    def test_selects_maximum_then_confirms_radio(self):
        self.capture.side_effect = [self.image, self.image, frame("points-eight")]
        self.assertTrue(cp.ChallengePointsSelect().run(self.context, self.args))
        self.click.assert_called_once_with(self.context, (876, 430))
        self.evidence.assert_called_once()

    def test_rereads_balance_each_round_and_downgrades(self):
        for points, expected in ((800, 4), (400, 2), (200, 1)):
            with self.subTest(points=points):
                self.read.return_value = points
                with patch.object(cp, "selected_multiplier", return_value=expected):
                    self.assertTrue(cp.ChallengePointsSelect().run(self.context, self.args))
        self.assertEqual(self.read.call_count, 6)
        self.click.assert_not_called()

    def test_insufficient_balance_cancels_without_start_or_completion(self):
        self.read.return_value = 199
        self.assertTrue(cp.ChallengePointsSelect().run(self.context, self.args))
        self.context.override_next.assert_called_with("ChallengePointSelect", ["ChallengePointsExhaustedRecover"])
        self.click.assert_called_once_with(self.context, cp.CANCEL_POINT)

    def test_unknown_and_unstable_balances_never_click(self):
        for readings in ([None] * 8, [1600, 200] * 4):
            with self.subTest(readings=readings):
                self.read.side_effect = readings
                self.assertFalse(cp.ChallengePointsSelect().run(self.context, self.args))
        self.click.assert_not_called()

    def test_changed_page_never_clicks(self):
        self.context.run_recognition.return_value.hit = False
        self.assertFalse(cp.ChallengePointsSelect().run(self.context, self.args))
        self.click.assert_not_called()

    def test_changed_balance_after_selection_does_not_confirm(self):
        self.read.side_effect = [1600, 1600, 1599]
        self.assertFalse(cp.ChallengePointsSelect().run(self.context, self.args))
        self.assertEqual(self.click.call_args.args[1], (876, 430))

    def test_unselected_radio_never_advances(self):
        self.assertFalse(cp.ChallengePointsSelect().run(self.context, self.args))
        self.assertEqual(self.click.call_count, 3)
        self.assertNotIn(cp.CONFIRM_POINT, [call.args[1] for call in self.click.call_args_list])

    def test_confirm_rechecks_affordability_and_radio(self):
        self.capture.return_value = frame("points-eight")
        self.assertTrue(cp.ChallengePointsConfirm().run(self.context, self.args))
        self.click.assert_called_once_with(self.context, cp.CONFIRM_POINT)
        self.click.reset_mock()
        self.read.return_value = 1599
        self.assertFalse(cp.ChallengePointsConfirm().run(self.context, self.args))
        self.click.assert_not_called()

    def test_cancel_during_capture_never_clicks(self):
        self.capture.side_effect = cp.ScreenRefreshCancelled("stopped")
        self.assertTrue(cp.ChallengePointsSelect().run(self.context, self.args))
        self.click.assert_not_called()

    def test_exhaustion_reports_actual_completed_count(self):
        reporting.clear_states()
        self.addCleanup(reporting.clear_states)
        argv = NS(node_name="ChallengeRoundGate", task_detail=NS(task_id=73, entry="ChallengeLive"),
                  custom_action_param=json.dumps({"total": 5, "task_name": "ChallengeLive", "phase": "initialize"}))
        with patch.object(reporting, "_visible_log", return_value=True) as visible:
            self.assertTrue(reporting.TaskProgress().run(self.context, argv))
            argv.custom_action_param = json.dumps({"status": "success", "completed_only": True,
                                                   "reason": "points exhausted"})
            self.assertTrue(reporting.TaskOutcome().run(self.context, argv))
            self.assertIn("0/5", visible.call_args.args[1])
            self.assertIn("points exhausted", visible.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
