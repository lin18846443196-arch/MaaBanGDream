"""Native video samples must not accelerate game-state frame-count guards."""

import queue
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_native_start import ROOT
from realtime.debug_recorder import RealtimeDebugRecorder
from realtime.engine import RealtimeEngine
from realtime.life_monitor import LifeGuard, LifeReading


class NativeVideoCadenceTests(unittest.TestCase):
    def run_native(self, video_enabled, *, capture_cost=.001, zero_after=None,
                   video_fps=60, duration=1.8):
        now = [0.]
        image = np.zeros((72, 128, 3), np.uint8)
        captures, samples, video, monitors = [], [], [], []

        def capture():
            now[0] += capture_cost
            captures.append(now[0])
            return image

        def detect(frame):
            samples.append(now[0])
            value = 0 if zero_after is not None and now[0] >= zero_after else 1000
            return LifeReading(True, value)

        def monitor(frame, timestamp):
            monitors.append(timestamp)
            return 'active'

        recorder = NS(
            video_enabled=video_enabled, video_fps=video_fps,
            record=Mock(), close=Mock(),
            record_phase=lambda frame, timestamp, phase, **kwargs:
                video.append((timestamp, phase, kwargs.get('diagnostics', []))),
        )
        backend = NS(
            exclusive=True, arm=Mock(), start=Mock(), stop=Mock(), poll=Mock(),
            observe_start_frame=Mock(return_value=.1), report=Mock(return_value={}),
        )
        touch = NS(close=Mock(), dispatch=Mock(), advance=Mock())
        detector = NS(detect=Mock())
        completion = NS(update=Mock(return_value=False))
        playfield = NS(active=True, mark_active=Mock(), observe=monitor)
        failed = NS(observe=Mock(return_value=False))
        engine = RealtimeEngine(
            detector, NS(timing_offset_ms=47), touch,
            clock=lambda: now[0], native_backend=backend, debug_recorder=recorder,
            life_detector=NS(detect=detect), life_guard=LifeGuard(),
            completion_guard=completion, playfield_monitor=playfield,
            live_failed_detector=failed,
        )
        with patch('realtime.engine.time.sleep', side_effect=lambda seconds:
                   now.__setitem__(0, now[0] + seconds)):
            stats = engine.run(
                capture, lambda: False, duration_seconds=duration, target_fps=60,
            )
        backend.start.assert_called_once_with(.1)
        backend.stop.assert_called_once()
        detector.detect.assert_not_called()
        touch.dispatch.assert_not_called()
        touch.advance.assert_not_called()
        self.assertEqual(completion.update.call_count, len(samples))
        self.assertEqual(failed.observe.call_count, len(samples))
        self.assertEqual(len(monitors), len(samples))
        return NS(stats=stats, captures=captures, samples=samples, video=video,
                  backend=backend, recorder=recorder)

    def test_full_video_captures_short_slide_frames_without_monitoring_every_frame(self):
        result = self.run_native(True)
        extra = [row for row in result.video if row[1] == 'native-diagnostic-video']
        self.assertGreater(len(extra), 75)
        self.assertLessEqual(len(result.samples), 10)
        self.assertGreaterEqual(len(result.samples), 8)
        self.assertEqual(result.backend.poll.call_count, len(result.samples) - 2)
        self.assertTrue(all(b-a >= .199 for a, b in zip(result.samples, result.samples[1:])))
        self.assertEqual(result.stats.final_timing_offset_ms, 47)
        cadence = [d for _, _, events in extra for d in events
                   if d['event'] == 'native-diagnostic-cadence']
        self.assertEqual(len(cadence), 1)
        self.assertEqual(cadence[0]['capture_target_fps'], 60)
        self.assertEqual(cadence[0]['monitor_interval_ms'], 200)

    def test_trace_only_retains_five_hz_capture(self):
        result = self.run_native(False)
        self.assertLessEqual(len(result.captures), 10)
        self.assertEqual(len(result.captures), len(result.samples))
        self.assertFalse(any(row[1] == 'native-diagnostic-video' for row in result.video))

    def test_three_zero_samples_still_cover_at_least_four_hundred_ms(self):
        results = [self.run_native(enabled, zero_after=.8) for enabled in (False, True)]
        for result in results:
            zeros = [stamp for stamp in result.samples if stamp >= .8]
            self.assertEqual(len(zeros), 3)
            self.assertGreaterEqual(zeros[-1] - zeros[0], .399)
            self.assertTrue(result.stats.aborted_for_life)
        self.assertLess(abs(results[0].samples[-1] - results[1].samples[-1]), .04)

    def test_slow_capture_skips_missed_video_deadlines_without_a_catchup_burst(self):
        result = self.run_native(True, capture_cost=.08)
        self.assertLessEqual(len(result.captures), 23)
        self.assertLessEqual(len(result.samples), 9)
        self.assertTrue(all(b-a >= .079 for a, b in zip(result.captures, result.captures[1:])))
        self.assertTrue(all(b-a >= .199 for a, b in zip(result.samples, result.samples[1:])))

    def test_video_sampling_respects_encoder_rate(self):
        result = self.run_native(True, video_fps=30)
        self.assertGreater(len(result.captures), 45)
        self.assertLessEqual(len(result.captures), 55)
        self.assertLessEqual(len(result.samples), 10)

    def test_existing_recorder_queue_drops_extra_frames_without_backpressure(self):
        recorder = object.__new__(RealtimeDebugRecorder)
        recorder._closed = False
        recorder._error = None
        recorder._first_timestamp = None
        recorder._dropped_trace_frames = 0
        recorder._record_queue = queue.Queue(maxsize=1)
        image = np.zeros((1, 1, 3), np.uint8)
        for index in range(100):
            recorder.record_phase(image, index / 60, 'native-diagnostic-video')
        self.assertEqual(recorder._record_queue.qsize(), 1)
        self.assertEqual(recorder._dropped_trace_frames, 99)


if __name__ == '__main__':
    unittest.main()
