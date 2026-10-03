import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import cv2
import numpy as np

from agent.realtime.chart_timeline import (
    ChartTimeline, ChartJudgement, ChartHoldPath, ChartPathPoint,
)
from agent.realtime.first_group_sync import (
    FirstGroupHeadDetector, FirstGroupSynchronizer, FirstGroupSyncConfig, ObservedHead,
)


FIXTURES = Path(__file__).parent / "fixtures"


class _Detector:
    def __init__(self):
        self.heads = []
        self.calls = 0

    def detect(self, image, timestamp):
        self.calls += 1
        return self.heads


class _Life:
    def __init__(self):
        self.value = 1000

    def detect(self, image):
        return SimpleNamespace(visible=True, value=self.value)


def _chart(events=None):
    return ChartTimeline(events or [ChartJudgement(2, 2, "tap", 0)])


class FirstGroupSynchronizerTests(unittest.TestCase):
    def setUp(self):
        self.detector = _Detector()
        self.life = _Life()
        self.image = np.zeros((720, 1280, 3), np.uint8)
        self.next_id = 0

    def gate(self, chart=None, **kwargs):
        return FirstGroupSynchronizer(chart or _chart(), detector=self.detector,
                                      playfield_detector=lambda image: True,
                                      popup_detector=lambda image: bool(image[0, 0, 0]),
                                      life_detector=self.life, **kwargs)

    def sample(self, now, *, new=True, capture_id=None, image=None, age_ms=1,
               uncertainty_ms=1, eligible=True):
        self.next_id += 1
        return SimpleNamespace(image=self.image if image is None else image,
                               captured_at=now, consumed_at=now + age_ms / 1000,
                               capture_id=self.next_id if capture_id is None else capture_id,
                               is_new=new, age_ms=age_ms, uncertainty_ms=uncertainty_ms,
                               eligible_for_start=eligible)

    def ready(self, gate, start=0):
        self.detector.heads = []
        for offset in (0, .03, .06):
            self.assertIsNone(gate.observe(self.sample(start + offset)))
        self.assertEqual(gate.state, "FirstGroupTracking")

    def moving(self, gate, *, lanes=(2,), kinds=("tap",), start=.09,
               extras=None, ys=(420, 450, 480)):
        prediction = None
        for index, y in enumerate(ys):
            self.detector.heads = [ObservedHead(lane, kind, y) for lane, kind in zip(lanes, kinds)]
            if extras:
                self.detector.heads.extend(extras(index, y))
            prediction = gate.observe(self.sample(start + index * .03)) or prediction
        return prediction

    def test_single_head_predicts_judgement_without_190ms(self):
        gate = self.gate()
        self.ready(gate)
        prediction = self.moving(gate)
        self.assertIsNotNone(prediction)
        self.assertAlmostEqual(prediction.first_due_s, .26, places=6)
        self.assertLess(prediction.uncertainty_ms, 20)
        self.assertGreaterEqual(prediction.confidence, .9)
        self.assertEqual(gate.state, "FirstGroupTracking")
        gate.mark_started()
        self.assertEqual(gate.state, "Playing")
        self.assertIsNone(gate.observe(self.sample(.2)))
        json.dumps(gate.report())

    def test_chord_requires_every_lane_and_consistent_crossing(self):
        chart = _chart([ChartJudgement(2, 1, "tap", 0), ChartJudgement(2, 5, "tap", 1)])
        gate = self.gate(chart)
        self.ready(gate)
        self.assertIsNone(self.moving(gate, lanes=(1,), kinds=("tap",)))
        self.assertEqual(gate.reason, "waiting-for-every-chord-head")
        gate = self.gate(chart)
        self.ready(gate)
        prediction = self.moving(gate, lanes=(1, 5), kinds=("tap", "tap"))
        self.assertEqual(prediction.lanes, (1, 5))

    def test_disagreeing_chord_never_locks(self):
        chart = _chart([ChartJudgement(2, 1, "tap", 0), ChartJudgement(2, 5, "tap", 1)])
        gate = self.gate(chart)
        self.ready(gate)
        prediction = self.moving(gate, lanes=(1,), kinds=("tap",),
                                 ys=(450, 480, 510),
                                 extras=lambda index, y: [ObservedHead(5, "tap", y-21)])
        self.assertIsNone(prediction)
        self.assertEqual(gate.reason, "chord-crossings-disagree")

    def test_flick_requires_flick_not_tap(self):
        gate = self.gate(_chart([ChartJudgement(2, 2, "tap", 0, flick=True)]))
        self.ready(gate)
        self.assertIsNone(self.moving(gate))
        self.assertIn("does-not-match", gate.rejected_reason)
        gate = self.gate(_chart([ChartJudgement(2, 2, "tap", 0, flick=True)]))
        self.ready(gate)
        self.assertIsNotNone(self.moving(gate, kinds=("flick",)))

    def test_hold_head_only_no_tail_or_internal_judgements(self):
        path = ChartHoldPath(1, "Slide", (
            ChartPathPoint(0, 2, 2), ChartPathPoint(1, 2.5, 3), ChartPathPoint(2, 3, 4),
        ))
        chart = ChartTimeline([
            ChartJudgement(2, 2, "hold-head", 1),
            ChartJudgement(2.5, 3, "slide-checkpoint", 1),
            ChartJudgement(3, 4, "hold-tail", 1),
        ], hold_paths=[path])
        gate = self.gate(chart)
        self.assertEqual(len(gate.prefix), 1)
        self.assertEqual(gate.prefix[0][0].kind, "hold")
        self.ready(gate)
        self.assertIsNotNone(self.moving(gate, kinds=("hold",)))

    def test_invalid_hold_path_or_empty_chart_refused(self):
        chart = ChartTimeline([ChartJudgement(2, 2, "hold-head", 1)], hold_paths=[])
        gate = self.gate(chart)
        self.assertIsNotNone(gate.rejected_reason)
        self.assertIsNone(gate.observe(self.sample(0)))

    def test_wrong_lane_leading_group_refused_without_later_rebase(self):
        gate = self.gate()
        self.ready(gate)
        self.assertIsNone(self.moving(gate, lanes=(4,)))
        self.assertIn("does-not-match", gate.rejected_reason)
        self.assertIsNone(self.moving(gate, start=.2))

    def test_duplicate_cache_ids_cannot_advance_readiness(self):
        gate = self.gate()
        first = self.sample(0, capture_id=1)
        gate.observe(first)
        gate.observe(self.sample(.03, capture_id=1))
        gate.observe(self.sample(.06, new=False, capture_id=1))
        self.assertEqual(gate.report()["ready_fresh_frames"], 1)
        self.assertEqual(gate.ignored_frames, 2)
        self.assertEqual(self.detector.calls, 1)

    def test_duplicate_and_stale_frames_cannot_advance_tracks(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        gate.observe(self.sample(.09, capture_id=100))
        for t, options in ((.12, {"capture_id": 100}), (.15, {"new": False}),
                           (.18, {"age_ms": 90}), (.21, {"uncertainty_ms": 45})):
            self.assertIsNone(gate.observe(self.sample(t, **options)))
        self.assertEqual(len(gate._tracks[(2, "tap")]), 1)

    def test_same_image_independent_capture_counts_as_fresh(self):
        gate = self.gate()
        self.ready(gate)
        self.assertEqual(gate.fresh_frames, 3)
        self.assertEqual(gate.ignored_frames, 0)

    def test_static_colored_component_never_predicts(self):
        gate = self.gate()
        self.ready(gate)
        self.assertIsNone(self.moving(gate, ys=(420, 420, 420)))
        self.assertIsNone(gate.report()["prediction"])

    def test_two_frames_or_insufficient_motion_never_predicts(self):
        gate = self.gate()
        self.ready(gate)
        self.assertIsNone(self.moving(gate, ys=(470, 480)))
        self.assertIsNone(gate.report()["prediction"])

    def test_large_capture_gap_after_track_refuses_later_group(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        gate.observe(self.sample(.09))
        gate.observe(self.sample(.29))
        self.assertEqual(gate.rejected_reason, "capture-gap-after-first-head-no-rebase")
        self.assertIsNone(self.moving(gate, start=.32))

    def test_50_100ms_sparse_samples_predict_but_200_400ms_refuse(self):
        for gap_ms in (50, 100, 200, 400):
            with self.subTest(gap_ms=gap_ms):
                gate = self.gate()
                self.ready(gate)
                result = None
                for index, y in enumerate((420, 470, 520)):
                    self.detector.heads = [ObservedHead(2, "tap", y)]
                    result = gate.observe(self.sample(.09+index*gap_ms/1000)) or result
                if gap_ms <= 100:
                    self.assertIsNotNone(result)
                else:
                    self.assertIsNone(result)
                    self.assertEqual(gate.rejected_reason, "capture-gap-after-first-head-no-rebase")

    def test_old_capture_id_cannot_advance_even_outside_recent_window(self):
        gate = self.gate()
        gate.observe(self.sample(0, capture_id=100))
        gate.observe(self.sample(.03, capture_id=99))
        gate.observe(self.sample(.06, capture_id=98))
        self.assertEqual(gate.report()["ready_fresh_frames"], 1)
        self.assertEqual(gate.ignored_frames, 2)

    def test_one_wrong_flash_cannot_bypass_late_first_head_guard(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(4, "tap", 450)]
        gate.observe(self.sample(.09))
        self.detector.heads = [ObservedHead(2, "tap", 520)]
        gate.observe(self.sample(.12))
        self.assertEqual(gate.rejected_reason, "unconfirmed-leading-head-identity-changed-no-rebase")

    def test_mismatch_then_fade_cannot_clear_first_candidate(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(4, "tap", 420)]
        gate.observe(self.sample(.09))
        faded = self.image.copy()
        faded[30:80, 950:1185] = 255
        gate.observe(self.sample(.12, image=faded))
        self.assertEqual(gate.rejected_reason, "ui-transition-during-first-group")
        self.assertIsNone(self.moving(gate, start=.15))

    def test_mismatch_then_blank_cannot_accept_matching_later_group(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(4, "tap", 420)]
        gate.observe(self.sample(.09))
        self.detector.heads = []
        gate.observe(self.sample(.12))
        self.assertEqual(gate.rejected_reason, "leading-unconfirmed-head-disappeared-no-rebase")
        self.assertIsNone(self.moving(gate, start=.15))

    def test_unconfirmed_leading_head_backward_jump_refuses(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(4, "tap", 450)]
        gate.observe(self.sample(.09))
        self.detector.heads = [ObservedHead(4, "tap", 420)]
        gate.observe(self.sample(.12))
        self.assertEqual(gate.rejected_reason, "leading-head-jumped-back-no-rebase")

    def test_directional_or_wide_first_flick_explicitly_unsupported(self):
        for direction, width in (("Left", 1), ("Right", 3), (None, 2)):
            with self.subTest(direction=direction, width=width):
                chart = _chart([ChartJudgement(2, 2, "tap", 0, flick=True,
                                               direction=direction, directional_width=width)])
                gate = self.gate(chart)
                self.assertEqual(gate.rejected_reason, "unsupported-first-head-direction-or-width")
                self.assertIsNone(gate.observe(self.sample(.06)))

    def test_gap_before_notes_requires_fresh_readiness(self):
        gate = self.gate()
        self.ready(gate)
        self.assertIsNone(gate.observe(self.sample(.4)))
        self.assertEqual(gate.state, "MembersReadyCandidate")
        self.assertEqual(gate.report()["ready_fresh_frames"], 1)

    def test_first_head_appearing_late_is_refused(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 500)]
        gate.observe(self.sample(.09))
        self.assertEqual(gate.rejected_reason, "first-head-first-seen-too-late")

    def test_life_drop_uses_fresh_frames_and_refuses(self):
        gate = self.gate()
        self.ready(gate)
        self.life.value = 800
        for t in (.16, .27, .38):
            gate.observe(self.sample(t))
        self.assertEqual(gate.rejected_reason, "life-dropped-before-first-group")

    def test_member_waiting_blocks_detection_and_reappearance_resets(self):
        gate = self.gate()
        waiting = self.image.copy()
        waiting[0, 0] = 1
        self.detector.heads = [ObservedHead(2, "tap", 450)]
        for t in (0, .03, .06):
            gate.observe(self.sample(t, image=waiting))
        self.assertEqual(gate.state, "MembersWaiting")
        self.assertEqual(self.detector.calls, 0)
        self.detector.heads = []
        for t in (.09, .12):
            gate.observe(self.sample(t))
        self.assertEqual(gate.state, "MembersReadyCandidate")
        gate.observe(self.sample(.15, image=waiting))
        self.assertEqual(gate.state, "MembersWaiting")
        self.ready(gate, .18)
        self.assertEqual(gate.member_wait_frames, 4)

    def test_popup_after_real_head_rejects_without_rebase(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        gate.observe(self.sample(.09))
        waiting = self.image.copy()
        waiting[0, 0] = 1
        gate.observe(self.sample(.12, image=waiting))
        self.assertEqual(gate.rejected_reason, "member-popup-after-first-head-no-rebase")
        self.assertIsNone(self.moving(gate, start=.24))

    def test_ui_fade_resets_ready_window(self):
        gate = self.gate()
        gate.observe(self.sample(0))
        changed = self.image.copy()
        changed[30:80, 950:1185] = 255
        gate.observe(self.sample(.03, image=changed))
        self.assertEqual(gate.report()["ready_fresh_frames"], 0)
        self.assertEqual(gate.reason, "ui-fade-readiness-reset")

    def test_no_clean_prelude_refuses_mid_song_entry(self):
        gate = self.gate()
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        for t in (0, .03, .06):
            gate.observe(self.sample(t))
        self.assertEqual(gate.rejected_reason, "note-present-before-members-readiness")

    def test_repetitive_prefix_remains_ambiguous(self):
        gate = self.gate(_chart([ChartJudgement(2, 2, "tap", 0),
                                 ChartJudgement(2.15, 2, "tap", 1)]))
        self.ready(gate)
        self.assertIsNone(self.moving(gate))
        self.assertEqual(gate.reason, "repetitive-prefix-needs-corroboration")

    def test_distinct_visible_prefix_disambiguates_repeated_group(self):
        chart = _chart([ChartJudgement(2, 2, "tap", 0),
                        ChartJudgement(2.1, 3, "tap", 1),
                        ChartJudgement(2.2, 2, "tap", 2),
                        ChartJudgement(2.3, 4, "tap", 3)])
        gate = self.gate(chart)
        self.ready(gate)
        prediction = self.moving(gate, extras=lambda index, y: [ObservedHead(3, "tap", y-100)])
        self.assertIsNotNone(prediction)

    def test_single_frame_prefix_flash_cannot_disambiguate(self):
        chart = _chart([ChartJudgement(2, 2, "tap", 0),
                        ChartJudgement(2.1, 3, "tap", 1),
                        ChartJudgement(2.2, 2, "tap", 2),
                        ChartJudgement(2.3, 4, "tap", 3)])
        gate = self.gate(chart)
        self.ready(gate)
        prediction = self.moving(gate, extras=lambda index, y: [ObservedHead(3, "tap", y-100)] if index == 2 else [])
        self.assertIsNone(prediction)
        self.assertEqual(gate.reason, "repetitive-prefix-needs-corroboration")

    def test_non_head_hold_tail_and_checkpoint_candidates_ignored(self):
        gate = self.gate()
        self.ready(gate)
        for index, y in enumerate((420, 450, 480)):
            self.detector.heads = [ObservedHead(2, "hold-tail", y),
                                   ObservedHead(2, "hold", y, is_head=False)]
            self.assertIsNone(gate.observe(self.sample(.09+index*.03)))
        self.assertFalse(gate._tracks)

    def test_playfield_lost_after_candidate_is_permanent_rejection(self):
        visible = [True]
        gate = FirstGroupSynchronizer(_chart(), detector=self.detector,
                                      playfield_detector=lambda image: visible[0],
                                      popup_detector=lambda image: False, life_detector=self.life)
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        gate.observe(self.sample(.09))
        visible[0] = False
        gate.observe(self.sample(.12))
        self.assertEqual(gate.rejected_reason, "playfield-lost-during-first-group")

    def test_capture_uncertainty_is_not_silently_discarded(self):
        gate = self.gate()
        self.ready(gate)
        for index, y in enumerate((420, 450, 480)):
            self.detector.heads = [ObservedHead(2, "tap", y)]
            prediction = gate.observe(self.sample(.09+index*.03, uncertainty_ms=19))
        self.assertIsNone(prediction)
        self.assertEqual(gate.reason, "prediction-uncertainty-too-large")

    def test_sparse_small_dropouts_keep_first_head_identity(self):
        gate = self.gate()
        self.ready(gate)
        self.detector.heads = [ObservedHead(2, "tap", 420)]
        gate.observe(self.sample(.09))
        self.detector.heads = []
        gate.observe(self.sample(.12))
        self.detector.heads = [ObservedHead(2, "tap", 480)]
        gate.observe(self.sample(.15))
        self.detector.heads = [ObservedHead(2, "tap", 510)]
        prediction = gate.observe(self.sample(.18))
        self.assertIsNotNone(prediction)
        self.assertAlmostEqual(prediction.first_due_s, .26)

    def test_input_not_published_by_sidecar(self):
        gate = self.gate()
        self.ready(gate)
        self.moving(gate)
        self.assertFalse(hasattr(gate, "publish"))
        with self.assertRaises(RuntimeError):
            self.gate().mark_started()

    def test_scale_normalized_before_detectors(self):
        seen = []
        gate = FirstGroupSynchronizer(_chart(), detector=self.detector,
                                     playfield_detector=lambda image: seen.append(image.shape) or True,
                                     popup_detector=lambda image: False, life_detector=self.life)
        for scale in (.75, 1, 1.5):
            with self.subTest(scale=scale):
                image = np.zeros((round(720*scale), round(1280*scale), 3), np.uint8)
                gate.observe(self.sample(scale, image=image))
                self.assertEqual(seen[-1], (720, 1280, 3))


class FirstGroupRealFixturesTests(unittest.TestCase):
    def test_pure_stage_lighting_does_not_extract_heads(self):
        for name in ("early-00.png", "early-trigger.png"):
            with self.subTest(name=name):
                image = cv2.imread(str(FIXTURES / "cooperative-20260929" / name))
                self.assertEqual(FirstGroupHeadDetector().detect(image, 0), [])

    def test_actual_type1_chord_detects_hold_and_skill_tap_before_band(self):
        image = cv2.imread(str(FIXTURES / "cooperative-20260929" / "early-01.png"))
        heads = FirstGroupHeadDetector().detect(image, 0)
        self.assertTrue(any(head.lane == 0 and head.kind == "hold" and 290 < head.y < 340 for head in heads))
        self.assertTrue(any(head.lane == 6 and head.kind == "tap" and 290 < head.y < 340 for head in heads))

    def test_real_waiting_members_never_calls_note_detector(self):
        detector = _Detector()
        detector.heads = [ObservedHead(2, "tap", 450)]
        gate = FirstGroupSynchronizer(_chart(), detector=detector)
        image = cv2.imread(str(FIXTURES / "cooperative-20261002" / "waiting-members.png"))
        for index in range(6):
            sample = SimpleNamespace(image=image, captured_at=index*.03,
                                     consumed_at=index*.03+.002, capture_id=index,
                                     is_new=True, age_ms=2, uncertainty_ms=1, eligible_for_start=True)
            self.assertIsNone(gate.observe(sample))
        self.assertEqual(gate.state, "MembersWaiting")
        self.assertEqual(detector.calls, 0)


if __name__ == "__main__":
    unittest.main()
