"""Independent timing equations, immutable evidence gate and offline replay."""

import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from realtime.startup_calibration import (
    StartupCalibration, StartupEnvironment, build_calibration_report,
    build_calibration_artifact, legacy_input_target_s, percentile, trajectory_input_target_s,
)
from realtime.chart_timeline import ChartJudgement, ChartTimeline
from realtime.first_group_sync import FirstGroupSynchronizer

_spec = importlib.util.spec_from_file_location("offline_native_start", ROOT / "tools/replay_native_start.py")
replay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay
_spec.loader.exec_module(replay)


ENV = StartupEnvironment(10.8, (1280, 720), 60, "type1")
OPENINGS = itertools.count()


def annotation(error_ms, **updates):
    result = {"predicted_first_due_s": 10.0, "annotated_first_due_s": 10.0 + error_ms / 1000,
              "estimated_uncertainty_ms": 3.0, "confidence": .97, "environment": ENV.to_dict()}
    result["opening_id"] = f"unit-test-opening-{next(OPENINGS)}"
    result.update(updates)
    return result


class CalibrationTests(unittest.TestCase):
    def test_new_due_never_receives_legacy_190ms(self):
        self.assertAlmostEqual(legacy_input_target_s(10, profile_advance_ms=47, device_first_read_ms=8), 10.135)
        self.assertAlmostEqual(trajectory_input_target_s(10, profile_advance_ms=47, device_first_read_ms=8), 9.945)
        self.assertAlmostEqual(trajectory_input_target_s(10, visual_phase_correction_ms=-12,
                                                       profile_advance_ms=47, device_first_read_ms=8), 9.933)

    def test_finite_inputs_and_signs(self):
        for value in (float("nan"), float("inf"), -float("inf"), None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                trajectory_input_target_s(10, visual_phase_correction_ms=value)
        self.assertAlmostEqual(trajectory_input_target_s(10, profile_advance_ms=-10), 10.01)

    def test_environment_requires_complete_signature_and_is_frozen(self):
        self.assertEqual(StartupEnvironment.from_mapping(ENV.to_dict()), ENV)
        with self.assertRaises(ValueError):
            StartupEnvironment.from_mapping({"resolution": [1280, 720], "fps": 60, "skin": "type1"})
        with self.assertRaises(ValueError):
            StartupEnvironment(10, (1280.0, 720), 60, "type1")
        with self.assertRaises(FrozenInstanceError):
            ENV.fps = 120

    def test_percentile_uses_linear_interpolation_not_rounded_index(self):
        self.assertEqual(percentile([0, 10], 95), 9.5)
        self.assertEqual(percentile([0, 10, 20, 30], 50), 15)
        self.assertIsNone(percentile([], 95))
        with self.assertRaises(ValueError):
            percentile([1], 101)

    def test_annotation_corrects_visual_residual_separate_from_profile(self):
        rows = [annotation(5 + value) for value in (-4, -3, -2, -1, 0, 0, 1, 2, 3, 4)]
        report = build_calibration_report(rows, externally_verified=True,
                                          device_execution_drifts_ms=[-.5, .5, 1])
        self.assertAlmostEqual(report["suggested_visual_phase_correction_ms"], 5)
        self.assertAlmostEqual(report["residual_ms"]["p95_abs"], 8.55)
        self.assertAlmostEqual(report["corrected_residual_ms"]["p95_abs"], 4)
        self.assertTrue(report["active_calibration_eligible"])
        calibration = StartupCalibration.from_report(report, evidence_source="independent-review")
        self.assertTrue(calibration.active_mode_allowed(ENV))
        self.assertEqual(report["device_execution_drift_ms"]["count"], 3)
        self.assertIn("none", report["profile_update"])
        with self.assertRaises(FrozenInstanceError):
            calibration.visual_phase_correction_ms = 99

    def test_exclusions_do_not_pool_different_environments(self):
        changed = StartupEnvironment(11, (1280, 720), 60, "type1")
        rows = [annotation(1) for _ in range(10)]
        rows += [annotation(900, confidence=.8), annotation(-500, estimated_uncertainty_ms=30),
                 annotation(600, environment=changed.to_dict()), annotation(float("nan"))]
        report = build_calibration_report(rows, expected_environment=ENV)
        self.assertEqual(report["accepted_count"], 10)
        self.assertEqual(report["excluded_count"], 4)
        self.assertAlmostEqual(report["suggested_visual_phase_correction_ms"], 1)
        self.assertFalse(report["active_calibration_eligible"])

    def test_active_gate_requires_verification_count_error_and_environment(self):
        default = StartupCalibration()
        self.assertFalse(default.active_mode_allowed(ENV))
        self.assertTrue(default.active_mode_allowed(ENV, allow_unverified=True))
        self.assertEqual(default.activation_reason(ENV, allow_unverified=True), "experimental-unverified-calibration")
        verified = StartupCalibration(2, True, ENV, 10, 20, "external-annotations", 3)
        self.assertTrue(verified.active_mode_allowed(ENV))
        self.assertFalse(verified.active_mode_allowed(None))
        self.assertFalse(verified.active_mode_allowed(StartupEnvironment(10.8, (1280, 720), 120, "type1")))
        for count, p95 in ((9, 2), (10, 20.01), (10, None)):
            self.assertFalse(StartupCalibration(2, True, ENV, count, p95, "external", 3).active_mode_allowed(ENV))

    def test_runtime_artifact_contract_and_environment_aliases(self):
        runtime_env = {"resolution": [1280, 720], "game_fps": 60, "note_speed": 10.8, "note_skin_type": 1}
        self.assertEqual(StartupEnvironment.from_mapping(runtime_env), ENV)
        report = build_calibration_report([annotation(4) for _ in range(10)], externally_verified=True)
        artifact = build_calibration_artifact(report, evidence_source="in-memory-unit-test-fixture")
        self.assertEqual(artifact["kind"], "native-start-calibration")
        self.assertTrue(artifact["validated"])
        self.assertEqual(artifact["environment"], runtime_env)
        self.assertEqual(artifact["sample_count"], 10)
        self.assertAlmostEqual(artifact["visual_phase_correction_ms"], 4)
        self.assertTrue(StartupCalibration.from_mapping(artifact).active_mode_allowed(ENV))

    def test_repeated_opening_annotations_are_not_ten_representative_samples(self):
        one_opening = annotation(2)
        report = build_calibration_report([one_opening] * 10, externally_verified=True)
        self.assertEqual(report["representative_count"], 1)
        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(report["exclusion_reasons"]["duplicate-opening-evidence"], 9)
        self.assertFalse(build_calibration_artifact(report, evidence_source="repeated-test") ["validated"])
        missing_ids = [annotation(2) for _ in range(10)]
        for row in missing_ids:
            row.pop("opening_id")
        report = build_calibration_report(missing_ids, externally_verified=True)
        self.assertEqual(report["accepted_count"], 10)
        self.assertEqual(report["representative_count"], 0)
        self.assertFalse(report["active_calibration_eligible"])

    def test_synthetic_evidence_and_unbounded_correction_cannot_export_validated(self):
        for rows in ([annotation(4, synthetic=True) for _ in range(10)],
                     [annotation(70) for _ in range(10)]):
            report = build_calibration_report(rows, externally_verified=True)
            self.assertTrue(report["acceptance_thresholds_passed"])
            artifact = build_calibration_artifact(report, evidence_source="synthetic-unit-test")
            self.assertFalse(artifact["validated"])

    def test_runtime_loader_cannot_ignore_synthetic_or_independent_opening_evidence(self):
        report = build_calibration_report([annotation(4) for _ in range(10)], externally_verified=True)
        artifact = build_calibration_artifact(report, evidence_source="in-memory-unit-test")
        artifact["contains_synthetic_evidence"] = True
        self.assertFalse(StartupCalibration.from_mapping(artifact).active_mode_allowed(ENV))
        artifact["contains_synthetic_evidence"] = False
        artifact["independent_opening_ids"] = ["one-opening"] * 10
        self.assertFalse(StartupCalibration.from_mapping(artifact).active_mode_allowed(ENV))
        uncertain = build_calibration_report([annotation(4, capture_uncertainty_ms=30) for _ in range(10)],
                                             externally_verified=True)
        self.assertEqual(uncertain["accepted_count"], 0)

    def test_device_receipts_are_not_annotation_truth(self):
        report = build_calibration_report([], externally_verified=True, device_execution_drifts_ms=[0] * 100)
        self.assertEqual(report["accepted_count"], 0)
        self.assertFalse(report["acceptance_thresholds_passed"])
        self.assertIsNone(report["suggested_visual_phase_correction_ms"])
        calibration = StartupCalibration.from_report(report, evidence_source="no-annotation-truth")
        self.assertEqual(calibration.visual_phase_correction_ms, 0)
        self.assertFalse(calibration.active_mode_allowed(ENV))

    def test_large_corrected_residual_and_too_few_samples_fail(self):
        for errors in ([0] * 9, [-50, 50] * 5):
            report = build_calibration_report([annotation(value) for value in errors], externally_verified=True)
            self.assertFalse(report["active_calibration_eligible"])


class ReplayTests(unittest.TestCase):
    def chart(self):
        return ChartTimeline([ChartJudgement(1, 2, "tap", 0), ChartJudgement(1.3, 4, "tap", 1)], bpm=120)

    def synthetic_frames(self, directory):
        # Exercise actual note extraction/trajectory fitting. Only playfield
        # identity is supplied, because this generated scene has no game UI.
        frames = []
        for index in range(22):
            timestamp = index * .04
            image = np.zeros((720, 1280, 3), np.uint8)
            if timestamp >= .4:
                y = round(590 - 480 * (1 - timestamp))
                x = round(640 + (490 - 640) * (y - 20) / 570)
                cv2.rectangle(image, (x - 30, y - 7), (x + 30, y + 7), (255, 210, 30), -1)
                cv2.rectangle(image, (x - 34, y - 10), (x + 34, y + 10), (255, 255, 255), 3)
            path = directory / f"frame-{index:03d}.png"
            cv2.imwrite(str(path), image)
            frames.append(replay.ManifestFrame(path, index, timestamp - .001, timestamp + .001,
                                                timestamp + .001, True, "synthetic-render", 1.0))
        return frames

    def factory(self, chart):
        return FirstGroupSynchronizer(chart, playfield_detector=lambda image: True,
                                      popup_detector=lambda image: False)

    def test_real_tracker_predicts_rendered_synthetic_trajectory_once(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = self.synthetic_frames(Path(directory))
            report = replay.replay_variant(self.chart(), frames, synchronizer_factory=self.factory)
        self.assertEqual(report["status"], "predicted-with-independent-annotation", report)
        self.assertLess(abs(report["prediction"]["error_ms"]), 6)
        self.assertTrue(report["dense_cadence_evidence"])
        self.assertEqual(sum(event["event"] == "first-group-predicted"
                             for event in report["tracker"]["events"]), 1)

    def test_repeats_do_not_add_fresh_evidence_or_change_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = self.synthetic_frames(Path(directory))
            plain = replay.replay_variant(self.chart(), frames, synchronizer_factory=self.factory)
            repeated = replay.replay_variant(self.chart(), frames, repeat_every=1,
                                             synchronizer_factory=self.factory)
        self.assertEqual(plain["prediction"], repeated["prediction"])
        self.assertEqual(plain["tracker"]["fresh_frames"], repeated["tracker"]["fresh_frames"])
        self.assertEqual(repeated["injected_cached_samples"], len(frames))

    def test_100_200_400ms_capture_stalls_refuse_later_rebase(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = self.synthetic_frames(Path(directory))
            for stall in (100, 200, 400):
                with self.subTest(stall=stall):
                    report = replay.replay_variant(self.chart(), frames, stall_ms=stall, stall_every=12,
                                                   synchronizer_factory=self.factory)
                    self.assertIsNone(report["prediction"], report)
                    self.assertIn("gap", report["tracker"]["rejected_reason"])
                    self.assertGreaterEqual(report["max_capture_uncertainty_ms"], stall / 2)

    def test_50ms_stall_keeps_uncertainty_and_original_annotation(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = self.synthetic_frames(Path(directory))
            report = replay.replay_variant(self.chart(), frames, stall_ms=50, stall_every=12,
                                           synchronizer_factory=self.factory)
        self.assertEqual(report["injected_stalls"], 1)
        self.assertGreaterEqual(report["max_capture_uncertainty_ms"], 25)
        self.assertEqual(report["error_count"], 0)
        if report["prediction"]:
            self.assertEqual(report["prediction"]["annotated_first_due_s"], 1.0)

    def test_drop_frames_report_missing_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = self.synthetic_frames(Path(directory))
            report = replay.replay_variant(self.chart(), frames, drop_every=2,
                                           synchronizer_factory=self.factory)
        self.assertEqual(report["dropped_samples"], len(frames) // 2)
        self.assertFalse(report["dense_cadence_evidence"])
        if report["prediction"]:
            self.assertLess(abs(report["prediction"]["error_ms"]), 10)

    @unittest.skipUnless((ROOT / "tests/fixtures/team/waiting-members.png").is_file(), "需要本地等待成员截图")
    def test_real_waiting_members_fixture_never_predicts(self):
        image = ROOT / "tests/fixtures/team/waiting-members.png"
        frames = [replay.ManifestFrame(image, index, index * .04 - .001, index * .04 + .001,
                                      index * .04 + .001, True, "real-wait-fixture") for index in range(8)]
        report = replay.replay_variant(self.chart(), frames)
        self.assertIsNone(report["prediction"])
        self.assertEqual(report["tracker"]["state"], "MembersWaiting")
        self.assertEqual(report["status"], "insufficient-evidence")

    def test_manifest_requires_intervals_and_explicit_freshness(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / "metadata.png"
        image.write_bytes(cv2.imencode(".png", np.zeros((720, 1280, 3), np.uint8))[1].tobytes())
        frames, report = replay.parse_manifest([{"image": str(image), "timestamp": 1, "fps": 60}], ROOT)
        self.assertFalse(frames)
        self.assertIn("capture-request-interval-or-freshness-missing", report["exclusion_reasons"])
        complete = {"file": str(image), "request_started_at": 1, "completed_at": 1.002,
                    "consumed_at": 1.003, "capture_id": "one", "is_new": True}
        frames, report = replay.parse_manifest([complete], ROOT)
        self.assertEqual(len(frames), 1)
        complete["completed_at"] = .9
        frames, report = replay.parse_manifest([complete], ROOT)
        self.assertFalse(frames)

    def test_historical_recording_does_not_invent_time_from_video_fps(self):
        directory = ROOT / "debug/recordings/coop-20261001-200759-477262"
        if not directory.is_dir():
            self.skipTest("optional local historical recording is not retained")
        rows, evidence = replay.load_recording(directory, max_rows=20000)
        frames, report = replay.parse_manifest(rows, directory)
        self.assertEqual(evidence["video_sidecar_interval_rows"], 0)
        self.assertFalse(frames)
        self.assertGreater(report["excluded_rows"], 0)

    def test_recording_auto_loads_new_native_startup_manifest(self):
        image_directory = tempfile.TemporaryDirectory()
        self.addCleanup(image_directory.cleanup)
        image = Path(image_directory.name) / "metadata.png"
        image.write_bytes(cv2.imencode(".png", np.zeros((720, 1280, 3), np.uint8))[1].tobytes())
        rows = [{"image": str(image), "capture_id": index, "is_new": True,
                 "request_started_at": index * .02, "completed_at": index * .02 + .002,
                 "consumed_at": index * .02 + .003, "source": "retained-runtime-startup-ring"}
                for index in range(12)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "native-startup.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            loaded, evidence = replay.load_recording(path, max_rows=20000)
            frames, manifest = replay.parse_manifest(loaded, path)
        self.assertEqual(evidence["source"], "recording-native-startup-manifest")
        self.assertEqual(evidence["native_startup_rows"], 12)
        self.assertEqual(manifest["usable_rows"], 12)
        self.assertEqual(frames[0].source, "retained-runtime-startup-ring")

    def test_bounded_json_reader_and_standalone_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "annotations.json"
            path.write_text(json.dumps({"externally_verified": True, "synthetic": True,
                                        "annotations": [annotation(2) for _ in range(10)]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                replay.read_rows(path, max_rows=5)
            artifact_path = Path(directory) / "synthetic-artifact.json"
            result = subprocess.run([sys.executable, str(ROOT / "tools/replay_native_start.py"),
                                     "--calibration", str(path), "--calibration-artifact-output", str(artifact_path)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["report"]["acceptance_thresholds_passed"])
            self.assertFalse(report["report"]["active_calibration_eligible"])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["kind"], "native-start-calibration")
            self.assertFalse(artifact["validated"])


if __name__ == "__main__":
    unittest.main()
