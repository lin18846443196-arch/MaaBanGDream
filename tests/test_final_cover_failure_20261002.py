"""Retain rejected cover identity and record bounded evidence, without input."""
import contextlib
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import final_cover as fc
from realtime import profile_play_action as pp
from realtime.chart_repository import LocalChartRepository
from realtime.song_identity import UNKNOWN_SONG_ID
from realtime.vision_io import imread_unicode


FINGERPRINT = 'song-jacket-phash-v2-c518cbb43dfa4e31'
OTHER_FINGERPRINT = 'song-jacket-phash-v2-3ae7344bc205b1ce'
LEVEL_CONFLICT = 'selected song level does not match local chart metadata'
UNCONFIRMED = 'song fingerprint is not confirmed'
MISSING = 'final cover jacket is not visible'


def identity(fingerprint=FINGERPRINT):
    return NS(song_id=fingerprint, method='recorded-final-jacket')


def selection(fingerprint=FINGERPRINT):
    return NS(difficulty='expert', level=27, title='独創収差',
              titles=('独創収差',), fingerprints=(fingerprint,),
              shared_jacket=False, bestdori_song_id=580)


class RetainedFailureTests(unittest.TestCase):
    def resolver(self, reason=LEVEL_CONFLICT, **kwargs):
        repository = Mock()
        repository.resolve.return_value = NS(selection=None, reason=reason)
        values = dict(difficulty='Expert', observed_level=27,
                      observed_title='独創収差👍👍🎊', observed_title_confidence=.71,
                      repository=repository)
        values.update(kwargs)
        return fc.FinalCoverResolver(**values), repository

    def test_actual_level_conflict_survives_waiting_member_page(self):
        resolver, repo = self.resolver()
        with patch.object(fc, 'identify_final_song', side_effect=[
            identity(), identity(), identity(UNKNOWN_SONG_ID),
        ]):
            self.assertIsNone(resolver.observe(object()))
            self.assertIsNone(resolver.observe(object()))
            self.assertEqual(resolver.last_reason, LEVEL_CONFLICT)
            self.assertIsNone(resolver.observe(object()))
        self.assertEqual(resolver.last_reason, MISSING)
        self.assertEqual(resolver.failure_reason, LEVEL_CONFLICT)
        self.assertEqual(resolver.failure_diagnostics['blocking_fingerprint'], FINGERPRINT)
        repo.resolve.assert_called_once_with(FINGERPRINT, 'expert', level=27,
                                             title='独創収差👍👍🎊')

    def test_difficulty_level_and_ambiguity_outweigh_later_hash_misses(self):
        for reason in (LEVEL_CONFLICT, 'difficulty conflicts with selected chart',
                       'song fingerprint mapping is ambiguous'):
            with self.subTest(reason=reason):
                resolver, repo = self.resolver(reason)
                repo.resolve.side_effect = [NS(selection=None, reason=reason),
                                            NS(selection=None, reason=UNCONFIRMED)]
                with patch.object(fc, 'identify_final_song', return_value=identity()):
                    for _ in range(3):
                        self.assertIsNone(resolver.observe(object()))
                self.assertEqual(resolver.last_reason, UNCONFIRMED)
                self.assertEqual(resolver.failure_reason, reason)

    def test_later_concrete_conflict_replaces_ordinary_unconfirmed_hash(self):
        resolver, repo = self.resolver(UNCONFIRMED)
        repo.resolve.side_effect = [NS(selection=None, reason=UNCONFIRMED),
                                    NS(selection=None, reason=LEVEL_CONFLICT)]
        with patch.object(fc, 'identify_final_song', return_value=identity()):
            for _ in range(3):
                resolver.observe(object())
        self.assertEqual(resolver.failure_reason, LEVEL_CONFLICT)

    def test_success_clears_retained_cause_and_failed_fingerprints(self):
        resolver, repo = self.resolver()
        repo.resolve.side_effect = [NS(selection=None, reason=LEVEL_CONFLICT),
                                    NS(selection=selection(), reason='confirmed')]
        with patch.object(fc, 'identify_final_song', return_value=identity()):
            resolver.observe(object())
            resolver.observe(object())
            result = resolver.observe(object())
        self.assertIsNotNone(result)
        self.assertEqual(resolver.failure_reason, 'confirmed')
        self.assertIsNone(resolver.failure_diagnostics['failed_fingerprint'])
        self.assertIsNone(resolver.failure_diagnostics['blocking_fingerprint'])
        with patch.object(fc, 'identify_final_song', return_value=identity(UNKNOWN_SONG_ID)):
            self.assertIsNone(resolver.observe(object()))
        self.assertEqual(resolver.failure_reason, MISSING)

    def test_selected_chart_gate_retains_mismatch_until_real_confirmation(self):
        resolver, _ = self.resolver(selection=selection(), repository=None)
        with patch.object(fc, 'identify_final_song', side_effect=[
            identity(OTHER_FINGERPRINT), identity(UNKNOWN_SONG_ID), identity(),
        ]):
            self.assertIsNone(resolver.observe(object()))
            mismatch = resolver.last_reason
            self.assertIsNone(resolver.observe(object()))
            self.assertEqual(resolver.failure_reason, mismatch)
            self.assertIsNotNone(resolver.observe(object()))
        self.assertEqual(resolver.failure_reason, 'confirmed')

    def test_absent_or_unstable_cover_has_no_retained_resolution_failure(self):
        resolver, repo = self.resolver()
        with patch.object(fc, 'identify_final_song', side_effect=[
            identity(), identity(UNKNOWN_SONG_ID), identity(OTHER_FINGERPRINT),
            identity(UNKNOWN_SONG_ID),
        ]):
            for _ in range(4):
                self.assertIsNone(resolver.observe(object()))
        self.assertEqual(resolver.failure_reason, MISSING)
        self.assertFalse(resolver.failure_diagnostics['resolution_attempted'])
        repo.resolve.assert_not_called()

    def test_failure_reason_property_is_read_only(self):
        resolver, _ = self.resolver()
        with self.assertRaises(AttributeError):
            resolver.failure_reason = 'ignore-level'

    def test_real_repository_keeps_wrong_preparation_level_rejected(self):
        image = imread_unicode(ROOT / 'tests/fixtures/cooperative-20261001-identity/cover-aokora-rhapsody.png')
        resolver = fc.FinalCoverResolver(repository=LocalChartRepository(ROOT / 'resource/charts'),
                                        difficulty='Expert', observed_level=99, observed_title=None)
        self.assertIsNone(resolver.observe(image))
        self.assertIsNone(resolver.observe(image))
        reason = resolver.failure_reason
        self.assertNotEqual(reason, MISSING)
        self.assertIsNone(resolver.observe(np.zeros_like(image)))
        self.assertEqual(resolver.failure_reason, reason)


class FinalCoverDiagnosticReplayTests(unittest.TestCase):
    def replay(self, frames, *, playfield=False, resolver=None, repository=None,
               timeout=2.0, require_black=False, require_title=False,
               frame_seconds=.05, recorder=None, identity_reader=None):
        clock = Clock()
        page = [None]
        sequence = iter(frames)
        controller = Mock()
        def capture():
            clock.advance(frame_seconds)
            page[0] = next(sequence, frames[-1])
            return page[0]
        controller.post_screencap.return_value.wait.return_value.get.side_effect = capture
        observations = []
        stages = set()
        def observer(image, timestamp, diagnostic):
            observations.append(dict(diagnostic))
            if recorder is not None:
                pp._record_final_cover_observation(recorder, stages, image, timestamp, diagnostic)
        def read_identity(image):
            code = int(image[0, 0, 0])
            if code in (40, 41):
                return identity()
            return identity(UNKNOWN_SONG_ID)
        def detect(image):
            return bool(playfield and int(image[0, 0, 0]) == 43)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fc, 'identify_final_song', side_effect=identity_reader or read_identity))
            stack.enter_context(patch.object(pp, 'PlayfieldDetector', return_value=detect))
            stack.enter_context(patch.object(pp, 'recognize_song_title', return_value=None))
            stack.enter_context(patch.object(pp.time, 'monotonic', clock.time))
            stack.enter_context(patch.object(pp.time, 'sleep', clock.advance))
            if resolver is not None:
                stack.enter_context(patch.object(pp, 'FinalCoverResolver', return_value=resolver))
            try:
                result = pp.wait_for_final_cover(controller,
                    NS(song_level=27, song_title='独創収差👍👍🎊', song_title_confidence=.71),
                    None, 'Expert', lambda: False, repository=repository,
                    timeout_seconds=timeout, poll_interval_seconds=0,
                    require_confirmed_chart=True, require_observed_title=require_title,
                    require_black_transition=require_black, observer=observer)
            except RuntimeError as exc:
                result = exc
        controller.post_click.assert_not_called()
        controller.post_click_key.assert_not_called()
        return result, observations

    def frame(self, code):
        return np.full((720, 1280, 3), code, dtype=np.uint8)

    def repo(self, reason=LEVEL_CONFLICT):
        repository = Mock()
        repository.resolve.return_value = NS(selection=None, reason=reason)
        return repository

    def test_terminal_stage_error_reports_original_level_conflict(self):
        result, observations = self.replay(
            [self.frame(40)] * 3 + [self.frame(43)] * 3,
            playfield=True, repository=self.repo())
        self.assertIsInstance(result, RuntimeError)
        self.assertIn(LEVEL_CONFLICT, str(result))
        self.assertNotIn(MISSING, str(result))
        self.assertEqual(observations[-1]['reason'], MISSING)
        self.assertEqual(observations[-1]['blocking_reason'], LEVEL_CONFLICT)
        self.assertEqual(observations[-1]['status'], 'identity-unconfirmed')

    def test_timeout_error_reports_original_level_conflict(self):
        result, _ = self.replay([self.frame(40)] * 3 + [self.frame(42)],
                                repository=self.repo(), timeout=1)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn(LEVEL_CONFLICT, str(result))

    def test_ordered_startup_timeout_also_retains_blocking_cause(self):
        result, _ = self.replay([self.frame(0), self.frame(40), self.frame(40), self.frame(42)],
                                repository=self.repo(), timeout=1,
                                require_black=True, frame_seconds=.15)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('启动阶段超时', str(result))
        self.assertIn(LEVEL_CONFLICT, str(result))

    def test_never_seen_cover_keeps_exact_missing_visibility_message(self):
        result, observations = self.replay([self.frame(43)], playfield=True,
                                           repository=self.repo())
        self.assertEqual(str(result), '最终封面谱面未确认，禁止沿用准备页候选谱面：' + MISSING)
        self.assertFalse(observations[-1]['resolution_attempted'])
        self.assertEqual(observations[-1]['status'], 'observing')

    def test_old_mock_resolver_without_new_properties_remains_supported(self):
        resolver = NS(evidence_reason=lambda: None, observe=lambda image: None,
                      last_reason=MISSING, frames=2, observed_title_confidence=1.0)
        result, _ = self.replay([self.frame(43)], playfield=True, resolver=resolver)
        self.assertEqual(str(result), '最终封面谱面未确认，禁止沿用准备页候选谱面：' + MISSING)

    def test_failure_checkpoint_includes_real_identity_and_only_saves_first_stage(self):
        recorder = Mock()
        result, observations = self.replay([self.frame(40)] * 4 + [self.frame(43)] * 3,
                                           playfield=True, repository=self.repo(), recorder=recorder)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(recorder.save_checkpoint.call_count, 2)
        calls = recorder.save_checkpoint.call_args_list
        self.assertEqual([call.args[2] for call in calls], ['observing', 'identity-unconfirmed'])
        details = calls[1].kwargs['details']
        self.assertEqual(details['reason'], LEVEL_CONFLICT)
        self.assertEqual(details['failed_fingerprint'], FINGERPRINT)
        self.assertEqual(details['observed_level'], 27)
        self.assertEqual(details['observed_title'], '独創収差👍👍🎊')
        self.assertEqual(details['observed_title_confidence'], .71)
        self.assertEqual(recorder.record_phase.call_count, len(observations))

    def test_six_seconds_of_changing_cover_hashes_do_not_write_each_frame(self):
        recorder = Mock()
        # Every animated fingerprint fails actual repository resolution, but
        # retains the same bounded identity-unconfirmed checkpoint stage.
        frames = [self.frame(40)] * 360
        stamps = iter(range(1000))
        def changing_identity(image):
            return identity(f'song-jacket-phash-v2-{next(stamps):016x}')
        with contextlib.redirect_stdout(io.StringIO()):
            result, observations = self.replay(frames, repository=self.repo(),
                recorder=recorder, timeout=6, frame_seconds=1 / 60,
                identity_reader=changing_identity)
        self.assertIsInstance(result, RuntimeError)
        self.assertGreaterEqual(len(observations), 350)
        self.assertGreater(len({item['failed_fingerprint'] for item in observations}), 300)
        self.assertEqual(recorder.save_checkpoint.call_count, 2)


if __name__ == '__main__':
    unittest.main()
