"""Input authority, provenance and immutable calibration boundaries; no device."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from realtime.frame_sample import FrameSample, FrameSampleStatistics
from realtime.native_play import NativeMinitouchBackend
from realtime.native_start_config import NativeStartSyncOptions, resolve_start_sync_options
from realtime.profile_store import RealtimeProfileStore
from realtime.engine import EngineStats
from realtime.run_reporting import result_report_payload


IMAGE = np.zeros((72, 128, 3), np.uint8)
ENV = {"resolution": [1280, 720], "game_fps": 60,
       "note_speed": 10.8, "note_skin_type": 1}


def sample(index=1, at=1.0, **changes):
    values = dict(image=IMAGE, capture_id=index, request_started_at=at - .002,
                  completed_at=at + .002, consumed_at=at + .004, is_new=True)
    values.update(changes)
    return FrameSample(**values)


def backend(mode="shadow", prediction=None, legacy=None, now=1.004):
    # Exercise the actual Native observer/dispatch boundary, with input-free
    # collaborators; constructing a device/thread would defeat offline tests.
    result = object.__new__(NativeMinitouchBackend)
    for name in ("sample_count", "new_samples", "reused_samples", "stale_samples", "invalid_samples"):
        setattr(result, "_startup_" + name, 0)
    result._startup_max_age_ms = result._startup_max_uncertainty_ms = 0
    result._startup_first_sample_s = result._startup_last_sample_s = None
    result._last_start_sample_id = result._last_start_sample_s = None
    result._startup_timing_boundaries = {}
    result._startup_anchor_source = result._startup_send_deadline_s = None
    result._legacy_anchor_candidate_s = None
    result._start_sync_mode = mode
    result._start_sync_error = None
    result._start_sync_options = {"visual_phase_correction_ms": -12, "mode": mode}
    result._start_sync = NS(observe=Mock(return_value=prediction),
                            rejected_reason=None, report=Mock(return_value={}), mark_started=Mock())
    result._photogate = NS(observe=Mock(return_value=legacy), _reset_band_state=Mock(),
                          report=Mock(return_value={}))
    result._clock = lambda: now
    result._frozen_timing_offset_ms = 47
    result._first_read_delay_s = .008
    result._confirm_start_device_ready = Mock()
    result._state = "ready"
    result._device = NS(connected=True)
    result._submit_session = Mock(return_value=True)
    result._session_state = "running"
    result._actions = []
    result._run_id = "offline"
    return result


def prediction(due=1.2, uncertainty=3):
    return NS(first_due_s=due, uncertainty_ms=uncertainty, confidence=.97)


class InputAuthorityTests(unittest.TestCase):
    def test_shadow_tracker_prediction_cannot_publish_anchor(self):
        b = backend(prediction=prediction())
        self.assertIsNone(b.observe_start_sample(sample()))
        b._confirm_start_device_ready.assert_not_called()
        b._submit_session.assert_not_called()

    def test_shadow_legacy_owns_anchor_with_bias_counted_once(self):
        b = backend(prediction=prediction(5), legacy=1.190)
        self.assertAlmostEqual(b.observe_start_sample(sample()), 1.135)
        self.assertEqual(b._startup_anchor_source, "fresh-frame-photogate")
        self.assertEqual(b._startup_timing_boundaries["legacy_visual_lead_ms"], 190)
        b._photogate.observe.assert_called_once_with(IMAGE, 1.004)

    def test_active_never_falls_back_to_legacy_if_tracker_waiting(self):
        b = backend("active", legacy=1.190)
        self.assertIsNone(b.observe_start_sample(sample()))
        b._confirm_start_device_ready.assert_not_called()

    def test_active_trajectory_never_adds_190_and_uses_correction_once(self):
        b = backend("active", prediction=prediction(), legacy=8)
        target = b.observe_start_sample(sample())
        self.assertAlmostEqual(target, 1.133)
        self.assertEqual(b._startup_timing_boundaries["legacy_visual_lead_ms"], 0)
        self.assertEqual(b._startup_timing_boundaries["visual_phase_correction_ms"], -12)
        with patch("builtins.print"):
            b.start(target)
        b._submit_session.assert_called_once_with("start", target)
        b._start_sync.mark_started.assert_called_once()

    def test_active_legacy_comparison_failure_cannot_block_good_tracker(self):
        b = backend("active", prediction=prediction())
        b._photogate.observe.side_effect = RuntimeError("old comparison")
        self.assertAlmostEqual(b.observe_start_sample(sample()), 1.133)

    def test_active_rejected_tracker_cannot_use_legacy(self):
        b = backend("active", legacy=1.190)
        b._start_sync.rejected_reason = "first-group-missed"
        with self.assertRaisesRegex(RuntimeError, "禁止中途启动"):
            b.observe_start_sample(sample())
        b._submit_session.assert_not_called()

    def test_active_requires_provenance_for_raw_frame_api(self):
        b = backend("active", legacy=1.190)
        with self.assertRaisesRegex(RuntimeError, "FrameSample"):
            b.observe_start_frame(IMAGE, 1)
        b._photogate.observe.assert_not_called()

    def test_cached_older_or_invalid_metadata_cannot_advance_either_gate(self):
        b = backend()
        b.observe_start_sample(sample(2))
        for row in (sample(2), sample(1), sample(3, is_new=False),
                    sample(4, completed_at=.5), sample(5, consumed_at=float("nan"))):
            self.assertIsNone(b.observe_start_sample(row))
        self.assertEqual(b._photogate.observe.call_count, 1)
        self.assertEqual(b._start_sync.observe.call_count, 1)
        self.assertEqual(b._startup_invalid_samples, 2)

    def test_stall_resets_old_stability_and_never_uses_stale_frame(self):
        b = backend()
        b.observe_start_sample(sample(1))
        b.observe_start_sample(sample(2, consumed_at=1.3))
        self.assertEqual(b._photogate.observe.call_count, 1)
        b._photogate._reset_band_state.assert_called_once()
        b.observe_start_sample(sample(3, at=1.4))
        self.assertEqual(b._photogate._reset_band_state.call_count, 2)
        self.assertAlmostEqual(b.start_gate_diagnostics()["startup_frames"]["new_fps"], 5)

    def test_regressing_sample_time_cannot_interpolate_a_new_start(self):
        b = backend("active")
        b.observe_start_sample(sample(1))
        with self.assertRaisesRegex(RuntimeError, "采集时间倒退"):
            b.observe_start_sample(sample(2, at=.9))
        self.assertEqual(b._photogate.observe.call_count, 1)
        b._submit_session.assert_not_called()

    def test_shadow_configuration_failure_is_diagnostic_only(self):
        for mode in ("shadow", "active"):
            b = backend(mode)
            b._state = "armed"
            b._chart_path = ROOT / "missing.json"
            options = NativeStartSyncOptions(mode=mode, calibration_verified=mode == "active")
            if mode == "active":
                with self.assertRaisesRegex(RuntimeError, "谱面同步配置失败"):
                    b.configure_start_sync(options)
            else:
                b.configure_start_sync(options)
                self.assertIsNone(b._start_sync)
                self.assertIn("configure", b._start_sync_error)

    def test_active_validates_uncertainty_and_final_lead_after_bias(self):
        for p in (prediction(1.1), prediction(1.2, 21), prediction(1.2, float("nan"))):
            with self.subTest(prediction=p):
                b = backend("active", prediction=p)
                with self.assertRaises(RuntimeError):
                    b.observe_start_sample(sample())
                b._confirm_start_device_ready.assert_not_called()

    def test_dispatch_rechecks_lead_and_exact_locked_anchor(self):
        for now, delta in ((1.104, 0), (1.004, .1)):
            b = backend("active", prediction=prediction())
            target = b.observe_start_sample(sample())
            b._clock = lambda: now
            with self.assertRaisesRegex(RuntimeError, "提前量不足或锚点无效"):
                b.start(target + delta)
            b._submit_session.assert_not_called()

    def test_device_not_ready_still_blocks_predicted_anchor(self):
        b = backend("active", prediction=prediction())
        b._confirm_start_device_ready.side_effect = RuntimeError("device not ready")
        with self.assertRaisesRegex(RuntimeError, "device not ready"):
            b.observe_start_sample(sample())
        b._submit_session.assert_not_called()

    def owner(self, b):
        b._observe_new_logs = Mock()
        b._require_probe = False
        b._probe_expected = set()
        b._observation_complete = Mock()
        b._session = NS(start=Mock(return_value=True), publish=Mock(return_value=True))
        b._refresh_session_snapshot = Mock(return_value="running")
        b._finish_when_fully_published = Mock()
        return b

    def test_owner_queue_wait_cannot_publish_expired_start(self):
        b = self.owner(backend("active", prediction=prediction()))
        target = b.observe_start_sample(sample())
        b._submit_session = lambda op, anchor: b._handle_owner_command(op, (anchor,))
        b._observe_new_logs.side_effect = lambda: setattr(b, "_clock", lambda: 1.2)
        with patch("realtime.native_play.native_engine.latency_calibrator", return_value=Mock()), \
             self.assertRaisesRegex(RuntimeError, "提前量不足"):
            b.start(target)
        b._session.start.assert_not_called()
        b._session.publish.assert_not_called()

    def test_session_preparation_cannot_consume_final_publish_margin(self):
        b = self.owner(backend("active", prediction=prediction()))
        target = b.observe_start_sample(sample())
        b._session.start.side_effect = lambda *args: setattr(b, "_clock", lambda: 1.12) or True
        with patch("realtime.native_play.native_engine.latency_calibrator", return_value=Mock()), \
             self.assertRaisesRegex(RuntimeError, "提前量不足"):
            b._handle_owner_command("start", (target,))
        b._session.start.assert_called_once_with(target)
        b._session.publish.assert_not_called()

    def test_owner_validated_start_publishes_once(self):
        b = self.owner(backend("active", prediction=prediction()))
        target = b.observe_start_sample(sample())
        with patch("realtime.native_play.native_engine.latency_calibrator", return_value=Mock()):
            self.assertTrue(b._handle_owner_command("start", (target,)))
        b._session.start.assert_called_once_with(target)
        b._session.publish.assert_called_once()
        self.assertGreater(b._startup_timing_boundaries["dispatch_send_lead_ms"], 30)


class CalibrationOptionsTests(unittest.TestCase):
    def artifact(self):
        return {"schema_version": 1, "kind": "native-start-calibration", "validated": True,
                "environment": dict(ENV), "sample_count": 10, "residual_p95_ms": 20,
                "visual_phase_correction_ms": -12, "max_capture_uncertainty_ms": 3,
                "independent_opening_ids": [f"run-{n}" for n in range(10)]}

    def resolve(self, value=None, **options):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "calibration.json").write_text(json.dumps(value or self.artifact()), encoding="utf-8")
            return resolve_start_sync_options({"native_start_sync_mode": "active",
                "native_start_calibration_file": "calibration.json", **options},
                run_mode="cooperative", profile_root=root, environment=ENV)

    def test_default_shadow_and_single_off_preserve_scope(self):
        for run_mode, expected in (("cooperative", "shadow"), ("formal", "off")):
            result = resolve_start_sync_options({}, run_mode=run_mode, profile_root=ROOT, environment=ENV)
            self.assertEqual(result.mode, expected)
        self.assertEqual(RealtimeProfileStore._validated_runtime_options({})["native_start_sync_mode"], "shadow")

    def test_only_verified_matching_calibration_can_activate(self):
        result = self.resolve()
        self.assertEqual(result.mode, "active")
        self.assertTrue(result.calibration_verified)
        self.assertEqual(result.visual_phase_correction_ms, -12)
        for key, value in (("validated", False), ("sample_count", 9), ("sample_count", True),
                           ("residual_p95_ms", 20.01), ("residual_p95_ms", float("nan")),
                           ("visual_phase_correction_ms", 61), ("visual_phase_correction_ms", True),
                           ("max_capture_uncertainty_ms", 21), ("max_capture_uncertainty_ms", None),
                           ("contains_synthetic_evidence", True), ("independent_opening_ids", ["same"] * 10)):
            artifact = self.artifact()
            artifact[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.resolve(artifact)
        for key, value in (("game_fps", 120), ("resolution", [1920, 1080]),
                           ("note_speed", 10.9), ("note_skin_type", 4)):
            artifact = self.artifact()
            artifact["environment"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolve(artifact)

    def test_active_requires_file_and_path_stays_within_profiles(self):
        for name in ("", "../calibration.json", "D:/outside.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.resolve(native_start_calibration_file=name)


class ProvenanceReportingTests(unittest.TestCase):
    def test_invalid_intervals_never_eligible_or_poison_statistics(self):
        stats = FrameSampleStatistics()
        for values in ({"request_started_at": float("nan")}, {"completed_at": .5},
                       {"completed_at": float("inf")}, {"consumed_at": .4},
                       {"capture_id": True}, {"request_started_at": -1}):
            row = sample(**values)
            self.assertFalse(row.eligible_for_start)
            json.dumps(row.diagnostics(), allow_nan=False)
            stats.observe(row)
        stats.observe(sample(2))
        stats.observe(sample(1, at=1.02))
        self.assertEqual(stats.report()["invalid_samples"], 6)
        self.assertEqual(stats.report()["new_samples"], 1)
        self.assertEqual(stats.report()["reused_samples"], 1)
        json.dumps(stats.report(), allow_nan=False)

    def test_result_json_retains_acquisition_diagnostics(self):
        stats = EngineStats(2, 0, False, capture_diagnostics={"new_samples": 1, "reused_samples": 1})
        report = result_report_payload(None, stats, timing_offset_ms=0, suggested_timing_offset_ms=None)
        self.assertEqual(report["capture_diagnostics"], stats.capture_diagnostics)


if __name__ == "__main__":
    unittest.main()
