"""Cached screenshots keep their acquisition sequence and host time interval."""

import unittest
from collections import deque

import numpy as np

from test_team_live import ROOT
from realtime.frame_sample import FrameSample, FrameSampleStatistics
from realtime.profile_play_action import StallSafeCapture


class Job:
    def __init__(self, image, *, done=True, error=None, on_wait=None):
        self.image = image
        self.done = done
        self.error = error
        self.on_wait = on_wait
        self.waits = 0
        self.gets = 0

    def wait(self):
        self.waits += 1
        if self.on_wait is not None:
            self.on_wait()
        self.done = True
        return self

    def get(self):
        self.gets += 1
        if self.error is not None:
            raise self.error
        return self.image


class Controller:
    def __init__(self, *jobs):
        self.jobs = deque(jobs)
        self.post_count = 0

    def post_screencap(self):
        self.post_count += 1
        return self.jobs.popleft() if self.jobs else Job(None, done=False)


class CaptureSampleTests(unittest.TestCase):
    def test_cache_preserves_id_and_time_while_consumption_advances(self):
        now = [2.0]
        image = np.zeros((2, 2, 3), np.uint8)
        pending = Job(image.copy(), done=False)
        capture = StallSafeCapture(Controller(Job(image), pending), clock=lambda: now[0])
        first = capture.sample()
        self.assertTrue(first.is_new)
        self.assertEqual(first.capture_id, 1)
        now[0] = 2.4
        cached = capture.sample()
        self.assertFalse(cached.is_new)
        self.assertEqual(cached.capture_id, first.capture_id)
        self.assertEqual(cached.captured_at, first.captured_at)
        self.assertIs(cached.image, image)
        self.assertAlmostEqual(cached.age_ms, 400.0)
        self.assertEqual(pending.waits, 0)
        self.assertEqual(capture.stall_count, 1)

    def test_equal_pixels_from_new_jobs_are_still_new_acquisitions(self):
        now = [0.0]
        image = np.zeros((2, 2, 3), np.uint8)
        pending = Job(image, done=False)
        capture = StallSafeCapture(Controller(Job(image), pending), clock=lambda: now[0])
        capture.sample()
        now[0] = .1
        pending.done = True
        second = capture.sample()
        self.assertTrue(second.is_new)
        self.assertEqual(second.capture_id, 2)
        self.assertEqual(second.completed_at, .1)
        self.assertEqual(second.captured_at, .05)
        self.assertEqual(second.request_started_at, 0.0)
        self.assertAlmostEqual(second.uncertainty_ms, 50.0)
        self.assertFalse(second.eligible_for_start)

    def test_legacy_call_returns_ndarray_and_exposes_metadata(self):
        image = np.zeros((2, 2, 3), np.uint8)
        capture = StallSafeCapture(Controller(Job(image)), clock=lambda: 1.0)
        self.assertIs(capture(), image)
        self.assertIs(capture.last_image, image)
        self.assertTrue(capture.last_sample.is_new)
        self.assertEqual(capture.report()['new_samples'], 1)

    def test_first_wait_reports_acquisition_interval(self):
        now = [10.0]
        image = np.zeros((2, 2, 3), np.uint8)
        first = Job(image, done=False, on_wait=lambda: now.__setitem__(0, 10.4))
        capture = StallSafeCapture(Controller(first), clock=lambda: now[0])
        sample = capture.sample()
        self.assertEqual(first.waits, 1)
        self.assertEqual(sample.request_started_at, 10.0)
        self.assertEqual(sample.completed_at, 10.4)
        self.assertEqual(sample.captured_at, 10.2)
        self.assertAlmostEqual(sample.age_ms, 200.0)
        self.assertAlmostEqual(sample.uncertainty_ms, 200.0)
        self.assertFalse(sample.eligible_for_start)
        self.assertEqual(sample.diagnostics()['time_basis'], 'request-completion-midpoint-estimate')

    def test_failed_job_reuses_provenance_without_claiming_new_frame(self):
        now = [0.0]
        image = np.zeros((2, 2, 3), np.uint8)
        failed = Job(None, done=False, error=OSError('capture interrupted'))
        capture = StallSafeCapture(Controller(Job(image), failed), clock=lambda: now[0])
        original = capture.sample()
        now[0] = .2
        failed.done = True
        sample = capture.sample()
        self.assertFalse(sample.is_new)
        self.assertEqual(sample.capture_id, original.capture_id)
        self.assertEqual(sample.captured_at, original.captured_at)

    def test_unavailable_first_image_is_rejected(self):
        capture = StallSafeCapture(Controller(Job(None), Job(None)), clock=lambda: 0.0)
        with self.assertRaisesRegex(RuntimeError, '截图未返回可用图像'):
            capture.sample()

    def test_statistics_do_not_count_cache_as_fps(self):
        image = np.zeros((2, 2, 3), np.uint8)
        stats = FrameSampleStatistics(capacity=3)
        for index in range(100):
            stats.observe(FrameSample(image, 1, 0.0, 0.0, index * .001, index == 0))
        stats.observe(FrameSample(image, 2, .15, .25, .25, True))
        report = stats.report()
        self.assertEqual(report['new_samples'], 2)
        self.assertEqual(report['reused_samples'], 99)
        self.assertEqual(report['retained_samples'], 3)
        self.assertEqual(report['percentile_scope'], 'recent_window')
        self.assertAlmostEqual(report['new_frame_fps'], 5.0)
        self.assertAlmostEqual(report['max_uncertainty_ms'], 50.0)
        self.assertAlmostEqual(report['max_new_frame_gap_ms'], 200.0)

    def test_duplicate_id_cannot_inflate_statistics_even_if_marked_new(self):
        image = np.zeros((2, 2, 3), np.uint8)
        stats = FrameSampleStatistics()
        stats.observe(FrameSample(image, 4, 1.0, 1.0, 1.0, True))
        stats.observe(FrameSample(image, 4, 1.1, 1.1, 1.1, True))
        self.assertEqual(stats.report()['new_samples'], 1)
        self.assertEqual(stats.report()['reused_samples'], 1)

    def test_start_eligibility_requires_fresh_low_age_and_low_uncertainty(self):
        image = np.zeros((2, 2, 3), np.uint8)
        self.assertTrue(FrameSample(image, 1, 0.0, .02, .02, True).eligible_for_start)
        self.assertFalse(FrameSample(image, 1, 0.0, .02, .02, False).eligible_for_start)
        self.assertFalse(FrameSample(image, 1, 0.0, .02, .11, True).eligible_for_start)
        self.assertFalse(FrameSample(image, 1, 0.0, .081, .081, True).eligible_for_start)


if __name__ == '__main__':
    unittest.main()
