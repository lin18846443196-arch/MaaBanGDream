"""Bounded pre-trigger source frames are flushed once by the record worker."""

import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT
from realtime.debug_recorder import RealtimeDebugRecorder
from realtime.engine import RealtimeEngine
from realtime.frame_sample import FrameSample
from realtime.vision_io import imread_unicode, imwrite_unicode

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.replay_native_start import parse_manifest, read_rows


def sample(capture_id, timestamp):
    return FrameSample(
        np.full((12, 16, 3), capture_id, np.uint8), capture_id,
        timestamp - .002, timestamp, timestamp + .001, True,
    )


class WorkerEvidenceTests(unittest.TestCase):
    def test_one_batch_retains_twelve_source_frames_and_replayable_metadata(self):
        caller_thread = threading.get_ident()
        writers = []

        def write(path, image):
            writers.append(threading.get_ident())
            return imwrite_unicode(path, image)

        with tempfile.TemporaryDirectory() as directory, patch(
            'realtime.debug_recorder.imwrite_unicode', side_effect=write,
        ):
            recorder = RealtimeDebugRecorder(Path(directory), video_enabled=False)
            frames = tuple(sample(index, index * .02) for index in range(1, 21))
            self.assertTrue(recorder.record_native_startup(frames, status='triggered'))
            self.assertFalse(recorder.record_native_startup(frames, status='rejected'))
            recorder.close()
            rows, metadata = read_rows(recorder.output_dir / 'native-startup.jsonl')
            parsed, result = parse_manifest(rows, recorder.output_dir, metadata=metadata)
            self.assertEqual(result['usable_rows'], 12)
            self.assertEqual([row['capture_id'] for row in rows], list(range(9, 21)))
            self.assertEqual(len(parsed), 12)
            for row in rows:
                self.assertEqual(row['time_basis'], 'request-completion-midpoint-estimate')
                self.assertEqual(row['source'], 'maa-screencap')
                self.assertTrue(row['is_new'])
                self.assertEqual(row['status'], 'triggered')
                image = imread_unicode(recorder.output_dir / row['image'])
                self.assertEqual(int(image[0, 0, 0]), row['capture_id'])
            summary = json.loads((recorder.output_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['native_startup_evidence_frames'], 12)
            self.assertEqual(summary['native_startup_manifest'], 'native-startup.jsonl')
            self.assertEqual(summary['dropped_native_startup_batches'], 1)
        self.assertTrue(writers)
        self.assertTrue(all(writer != caller_thread for writer in writers))

    def test_queue_pressure_drops_one_batch_without_encoding_or_backpressure(self):
        recorder = object.__new__(RealtimeDebugRecorder)
        recorder._closed = False
        recorder._error = None
        recorder._native_startup_enqueued = False
        recorder._dropped_native_startup_batches = 0
        recorder._record_queue = queue.Queue(maxsize=1)
        recorder._record_queue.put_nowait('busy')
        with patch('realtime.debug_recorder.imwrite_unicode') as write:
            self.assertFalse(recorder.record_native_startup((sample(1, 1.0),), status='timeout'))
            write.assert_not_called()
        self.assertEqual(recorder._record_queue.qsize(), 1)
        self.assertEqual(recorder._dropped_native_startup_batches, 1)

    def test_failed_evidence_write_does_not_stop_normal_trace_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            'realtime.debug_recorder.imwrite_unicode', side_effect=OSError('test disk unavailable'),
        ):
            recorder = RealtimeDebugRecorder(Path(directory), video_enabled=False)
            frame = sample(1, 1.0)
            self.assertTrue(recorder.record_native_startup((frame,), status='rejected', reason='original gate rejection'))
            recorder.record_phase(frame.image, 1.0, 'after-evidence-failure')
            recorder.close()
            self.assertIsNone(recorder._error)
            self.assertEqual(recorder._trace_frames, 1)
            self.assertIn('test disk unavailable', recorder._native_startup_error)
            summary = json.loads((recorder.output_dir / 'summary.json').read_text(encoding='utf-8'))
            self.assertIn('test disk unavailable', summary['native_startup_evidence_error'])
            self.assertIsNone(summary['recorder_error'])


class EngineEvidenceTests(unittest.TestCase):
    def run_engine(self, outcome, *, enqueue_error=False, regress_ids=False):
        now = [0.0]
        captures = []

        class Capture:
            last_sample = None

            def __call__(self):
                now[0] += .003
                index = len(captures) + 1
                capture_id = index
                if regress_ids and index in (4, 5):
                    capture_id = 3 if index == 4 else 2
                self.last_sample = sample(capture_id, now[0])
                # Source consumption cannot be ahead of the injected engine clock.
                self.last_sample = FrameSample(
                    self.last_sample.image, capture_id, now[0] - .002,
                    now[0], now[0], True,
                )
                captures.append(self.last_sample)
                return self.last_sample.image

        def observe(frame):
            if len(captures) == 20 and outcome == 'rejected':
                raise RuntimeError('original gate rejection')
            return .5 if len(captures) == 20 and outcome == 'triggered' else None

        backend = NS(
            exclusive=True, arm=Mock(), start=Mock(), stop=Mock(), poll=Mock(),
            observe_start_sample=Mock(side_effect=observe),
            observe_start_frame=Mock(return_value=None), report=Mock(return_value={}),
        )
        recorder = NS(video_enabled=False, record=Mock(), record_phase=Mock(),
                      close=Mock(), record_native_startup=Mock(return_value=True))
        if enqueue_error:
            recorder.record_native_startup.side_effect = OSError('enqueue unavailable')
        touch = NS(close=Mock(), dispatch=Mock(), advance=Mock())
        engine = RealtimeEngine(
            NS(detect=Mock()), NS(timing_offset_ms=0), touch,
            clock=lambda: now[0], native_backend=backend, debug_recorder=recorder,
        )
        with patch('realtime.engine.time.sleep', side_effect=lambda seconds:
                   now.__setitem__(0, now[0] + seconds)):
            if outcome == 'rejected':
                with self.assertRaisesRegex(RuntimeError, '^original gate rejection$') as rejected:
                    engine.run(Capture(), lambda: False, duration_seconds=1.2,
                               target_fps=60, startup_timeout_seconds=1.0)
                stats = rejected.exception.realtime_stats
            else:
                stats = engine.run(
                    Capture(), lambda: outcome == 'stopped' and now[0] >= .2,
                    duration_seconds=1.2, target_fps=60, startup_timeout_seconds=1.0,
                )
        touch.dispatch.assert_not_called()
        touch.advance.assert_not_called()
        return NS(stats=stats, backend=backend, recorder=recorder, captures=captures)

    def test_trigger_flushes_only_last_twelve_fresh_source_frames(self):
        result = self.run_engine('triggered', regress_ids=True)
        result.recorder.record_native_startup.assert_called_once()
        call = result.recorder.record_native_startup.call_args
        self.assertEqual(call.kwargs['status'], 'triggered')
        self.assertEqual([frame.capture_id for frame in call.args[0]], list(range(9, 21)))
        self.assertTrue(all(frame.is_new for frame in call.args[0]))
        observed = [call.args[0] for call in result.backend.observe_start_sample.call_args_list]
        self.assertFalse(observed[3].is_new)
        self.assertFalse(observed[4].is_new)
        result.backend.start.assert_called_once_with(.5)

    def test_rejection_flush_cannot_replace_original_failure(self):
        result = self.run_engine('rejected', enqueue_error=True)
        result.recorder.record_native_startup.assert_called_once()
        self.assertEqual(result.recorder.record_native_startup.call_args.kwargs['status'], 'rejected')
        evidence = result.stats.capture_diagnostics['startup_evidence']
        self.assertFalse(evidence['enqueue_accepted'])
        self.assertIn('enqueue unavailable', evidence['enqueue_error'])
        self.assertIn('original gate rejection', result.stats.terminal_reason)
        result.backend.start.assert_not_called()

    def test_timeout_flushes_once_and_retains_capture_intervals(self):
        result = self.run_engine('timeout')
        self.assertTrue(result.stats.startup_timed_out)
        call = result.recorder.record_native_startup.call_args
        self.assertEqual(result.recorder.record_native_startup.call_count, 1)
        self.assertEqual(call.kwargs['status'], 'timeout')
        self.assertEqual(len(call.args[0]), 12)
        self.assertTrue(all(frame.request_started_at <= frame.completed_at <= frame.consumed_at
                            for frame in call.args[0]))
        result.backend.start.assert_not_called()

    def test_user_stop_clears_ring_without_flushing_or_publishing_input(self):
        result = self.run_engine('stopped')
        self.assertTrue(result.stats.stopped)
        result.recorder.record_native_startup.assert_not_called()
        result.backend.start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
