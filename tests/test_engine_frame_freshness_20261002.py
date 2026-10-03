"""Input ownership stays independent while reused frames cannot confirm state."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT
from realtime.engine import RealtimeEngine
from realtime.frame_sample import FrameSample
from realtime.life_monitor import LifeGuard, LifeReading


class SampleCapture:
    def __init__(self, now, *, always_fresh=False):
        self.now = now
        self.count = 0
        self.image = np.zeros((72, 128, 3), np.uint8)
        self.always_fresh = always_fresh
        self.last_sample = None
        self.first_at = None

    def __call__(self):
        self.now[0] += .001
        self.count += 1
        if self.first_at is None:
            self.first_at = self.now[0]
        timestamp = self.now[0] if self.always_fresh else self.first_at
        self.last_sample = FrameSample(
            self.image, self.count if self.always_fresh else 1,
            timestamp, timestamp, self.now[0],
            self.always_fresh or self.count == 1,
        )
        return self.image


class EngineFreshnessTests(unittest.TestCase):
    def run_engine(self, *, start=False, stopping_at=None, old_backend=False):
        now = [0.0]
        capture = SampleCapture(now)
        backend = NS(
            exclusive=True, arm=Mock(), start=Mock(), stop=Mock(), poll=Mock(),
            report=Mock(return_value={}),
            observe_start_frame=Mock(return_value=.1 if start else None),
            start_gate_diagnostics=Mock(return_value={'photogate_popup_active': False}),
        )
        if not old_backend:
            backend.observe_start_sample = Mock(return_value=.1 if start else None)
        touch = NS(close=Mock(), dispatch=Mock(), advance=Mock())
        detector = NS(detect=Mock())
        life = NS(detect=Mock(return_value=LifeReading(True, 0)))
        guard = LifeGuard(confirm_frames=3)
        if start:
            for _ in range(3):
                guard.update(LifeReading(True, 1000))
        completion = NS(update=Mock(return_value=False))
        monitor = NS(active=True, mark_active=Mock(), observe=Mock(return_value='active'))
        failed = NS(observe=Mock(return_value=False))
        recorder = NS(video_enabled=False, record=Mock(), record_phase=Mock(), close=Mock())
        engine = RealtimeEngine(
            detector, NS(timing_offset_ms=0), touch, clock=lambda: now[0],
            native_backend=backend, life_detector=life, life_guard=guard,
            completion_guard=completion, playfield_monitor=monitor,
            live_failed_detector=failed, debug_recorder=recorder,
        )
        with patch('realtime.engine.time.sleep', side_effect=lambda seconds:
                   now.__setitem__(0, now[0] + seconds)):
            stats = engine.run(
                capture, lambda: stopping_at is not None and now[0] >= stopping_at,
                duration_seconds=1.5, target_fps=60, startup_timeout_seconds=1.0,
            )
        touch.dispatch.assert_not_called()
        touch.advance.assert_not_called()
        detector.detect.assert_not_called()
        backend.stop.assert_called_once()
        return NS(stats=stats, backend=backend, capture=capture, recorder=recorder,
                  life=life, guard=guard, completion=completion, monitor=monitor, failed=failed)

    def test_sample_protocol_receives_cache_for_diagnostics_without_starting(self):
        result = self.run_engine()
        self.assertTrue(result.stats.startup_timed_out)
        result.backend.start.assert_not_called()
        result.backend.observe_start_frame.assert_not_called()
        observed = [row.args[0] for row in result.backend.observe_start_sample.call_args_list]
        self.assertGreater(len(observed), 50)
        self.assertEqual(sum(sample.is_new for sample in observed), 1)
        self.assertGreater(result.stats.capture_diagnostics['reused_samples'], 50)
        result.life.detect.assert_not_called()
        events = [event for row in result.recorder.record_phase.call_args_list
                  for event in row.kwargs.get('diagnostics', [])]
        gate_events = [event for event in events if event['event'] == 'native-first-note-gate']
        self.assertTrue(gate_events)
        self.assertIn('frame_sample', gate_events[0])

    def test_started_native_polls_without_recounting_cached_zero_life(self):
        result = self.run_engine(start=True)
        result.backend.start.assert_called_once_with(.1)
        self.assertGreater(result.backend.poll.call_count, 3)
        self.assertEqual(result.life.detect.call_count, 1)
        self.assertEqual(result.guard.zero_streak, 1)
        self.assertFalse(result.stats.life_depleted)
        self.assertFalse(result.stats.aborted_for_life)
        self.assertEqual(result.completion.update.call_count, 1)
        self.assertEqual(result.monitor.observe.call_count, 1)
        self.assertEqual(result.failed.observe.call_count, 1)

    def test_stop_before_start_keeps_input_unpublished(self):
        result = self.run_engine(stopping_at=.25)
        self.assertTrue(result.stats.stopped)
        self.assertFalse(result.stats.startup_timed_out)
        result.backend.start.assert_not_called()
        result.backend.poll.assert_not_called()

    def test_older_backend_cannot_advance_gate_with_reused_screenshots(self):
        result = self.run_engine(old_backend=True)
        self.assertTrue(result.stats.startup_timed_out)
        result.backend.observe_start_frame.assert_called_once()
        result.backend.start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
