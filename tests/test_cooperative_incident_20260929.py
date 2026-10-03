"""Offline replay of September 29 failures; no live input or network changes."""
import copy
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import cv2
import numpy as np

# Imports the real actions with AgentServer registration disabled for tests.
from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime import profile_play_action as pp
from realtime.chart_repository import LocalChartRepository
from realtime.final_cover import FinalCoverResolver
from realtime import final_cover as fc
from realtime.native_play import NativeStartPhotogate
from realtime.life_monitor import LifeReading
from realtime.vision_io import imread_unicode

FIXTURES = ROOT / "tests/fixtures/cooperative-20260929"
FINGERPRINT = "song-jacket-phash-v2-ee919d629d2bf00e"


class ChartIdentityTests(unittest.TestCase):
    def setUp(self):
        self.repo = LocalChartRepository(ROOT / "resource/charts")

    def test_recorded_marina_cover_resolves_equivalent_aliases(self):
        result = self.repo.resolve(FINGERPRINT, "Expert", level=25,
                                   title="ときめきエクスペリエンス！")
        self.assertIsNotNone(result.selection, result.reason)
        self.assertIn(result.selection.bestdori_song_id, (786, 790))
        self.assertEqual(result.selection.expected_notes, 632)
        self.assertTrue(result.selection.shared_jacket_level_unique)

    def test_final_cover_replaces_original_title_candidate(self):
        original = self.repo.resolve("unknown", "Expert", level=25,
                                     title="ときめきエクスペリエンス！")
        self.assertEqual(original.selection.bestdori_song_id, 24)
        resolver = FinalCoverResolver(repository=self.repo, difficulty="Expert",
            observed_level=25, observed_title="ときめきエクスペリエンス！")
        with patch.object(fc, "identify_final_song",
                          return_value=NS(song_id=FINGERPRINT, method="test")):
            self.assertIsNone(resolver.observe(object()))
            result = resolver.observe(object())
        self.assertIsNotNone(result, resolver.last_reason)
        self.assertIn(result.selection.bestdori_song_id, (786, 790))

    def test_different_playable_charts_must_remain_ambiguous(self):
        manifest = self.repo._load_manifest()
        for song in manifest["songs"]:
            if song["bestdori_song_id"] == 790:
                song["difficulties"]["expert"]["chart_sha256"] = "f" * 64
        with patch.object(self.repo, "_load_manifest", return_value=manifest):
            result = self.repo.resolve(FINGERPRINT, "Expert", level=25)
        self.assertIsNone(result.selection)
        self.assertIn("ambiguous", result.reason)

    def test_missing_digest_does_not_prove_equivalence(self):
        manifest = self.repo._load_manifest()
        for song in manifest["songs"]:
            if song["bestdori_song_id"] in (786, 790):
                song["difficulties"]["expert"]["chart_sha256"] = ""
        with patch.object(self.repo, "_load_manifest", return_value=manifest):
            self.assertIsNone(self.repo.resolve(FINGERPRINT, "Expert", level=25).selection)

    def test_alias_with_missing_difficulty_does_not_mask_available_chart(self):
        result = self.repo.resolve(FINGERPRINT, "Special", level=26)
        self.assertIsNotNone(result.selection, result.reason)
        self.assertEqual(result.selection.bestdori_song_id, 786)

    def test_original_song_and_three_successful_songs_stay_unchanged(self):
        for song_id in (24, 514, 177, 712):
            song = next(s for s in self.repo._load_manifest()["songs"]
                        if s["bestdori_song_id"] == song_id)
            entry = song["difficulties"]["expert"]
            result = self.repo.resolve(song["fingerprints"][0], "Expert",
                                       level=entry["level"])
            self.assertIsNotNone(result.selection, result.reason)
            self.assertEqual(result.selection.bestdori_song_id, song_id)


class StrictCoverTests(unittest.TestCase):
    def run_cover(self, *, playfield, strict=True, confirmed=None):
        image = np.full((720, 1280, 3), 40, np.uint8)
        resolver = Mock(last_reason="final cover jacket does not match selected chart",
                        frames=2, observed_title_confidence=1.)
        resolver.evidence_reason.return_value = None
        resolver.observe.return_value = confirmed
        controller = Mock()
        controller.post_screencap.return_value.wait.return_value.get.return_value = image
        ticks = iter(np.arange(0., 4., .1))
        with patch.object(pp, "FinalCoverResolver", return_value=resolver), \
             patch.object(pp, "PlayfieldDetector", return_value=lambda image: playfield), \
             patch.object(pp.time, "monotonic", side_effect=lambda: next(ticks)), \
             patch.object(pp.time, "sleep"):
            return pp.wait_for_final_cover(controller,
                NS(song_level=25, song_title="test", song_title_confidence=1.),
                object(), "Expert", lambda: False, timeout_seconds=1,
                require_confirmed_chart=strict)

    def test_playfield_cannot_allow_unconfirmed_candidate(self):
        with self.assertRaisesRegex(RuntimeError, "禁止沿用"):
            self.run_cover(playfield=True)

    def test_timeout_cannot_allow_unconfirmed_candidate(self):
        with self.assertRaisesRegex(RuntimeError, "禁止沿用"):
            self.run_cover(playfield=False)

    def test_confirmed_chart_passes_strict_gate(self):
        result = NS(confirmation=NS(bestdori_song_id=786))
        self.assertIs(self.run_cover(playfield=False, confirmed=result).resolution, result)

    def test_explicit_legacy_fallback_is_not_changed(self):
        self.assertEqual(self.run_cover(playfield=True, strict=False).status,
                         "degraded-selected-chart")


class FirstNoteReplayTests(unittest.TestCase):
    def gate(self):
        gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed",
            playfield_detector=lambda image: True, suppress_prepare_popup=False,
            stable_duration_ms=120)
        gate._startup_life = None  # separately tested; isolate timing evidence
        return gate

    def image(self, name):
        result = imread_unicode(FIXTURES / name)
        self.assertIsNotNone(result, name)
        return result

    def prime(self, gate):
        blank = np.zeros((720, 1280, 3), np.uint8)
        for stamp in np.arange(0., .9, 1/60):
            self.assertIsNone(gate.observe(blank, float(stamp)))

    def test_actual_early_trigger_and_following_frames_are_rejected(self):
        gate = self.gate()
        self.prime(gate)
        for i, name in enumerate(("early-trigger.png", "early-00.png",
                                   "early-01.png", "early-02.png")):
            self.assertIsNone(gate.observe(self.image(name), 1. + .2 * i), name)
        self.assertFalse(gate.triggered)
        self.assertTrue(any(e["event"] == "note-head-missing"
                            for e in gate.report()["photogate_events"]))
        self.assertIsNotNone(gate.observe(self.image("first-real-notes.png"), 1.8))
        self.assertTrue(gate.triggered)

    def test_three_recorded_successes_still_trigger_without_extra_frame(self):
        for name in ("success-mania.png", "success-queen.png", "success-sakura.png"):
            with self.subTest(name=name):
                gate = self.gate()
                self.prime(gate)
                self.assertIsNotNone(gate.observe(self.image(name), 1.))
                self.assertEqual(gate.triggered_at_s, 1.)

    def test_scaled_input_keeps_positive_and_negative_evidence(self):
        gate = self.gate()
        for scale in (.75, 1., 1.5):
            for name, expected in (("success-queen.png", True), ("early-trigger.png", False)):
                img = cv2.resize(self.image(name), None, fx=scale, fy=scale)
                self.assertEqual(gate._has_approaching_note_head(img), expected)

    def test_life_loss_before_anchor_stops_cooperative_input(self):
        gate = NativeStartPhotogate(mode="cooperative-playfield-confirmed",
            playfield_detector=lambda image: True, popup_detector=lambda image: True)
        gate._startup_life = NS(detect=Mock(side_effect=[
            LifeReading(True, v) for v in (1000, 900, 900, 900)]))
        image = self.image("early-trigger.png")
        for stamp in (0., .11, .22):
            self.assertIsNone(gate.observe(image, stamp))
        with self.assertRaisesRegex(RuntimeError, "拒绝从歌曲中段启动"):
            gate.observe(image, .33)
        self.assertFalse(gate.triggered)


class CooperativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.controller = Mock()
        self.controller.post_stop_app.return_value.wait.return_value = NS(succeeded=True)
        self.controller.post_start_app.return_value.wait.return_value = NS(succeeded=True)
        self.context = NS(tasker=NS(stopping=False, controller=self.controller))
        self.settings = dict(ca.DEFAULT_SETTINGS, disconnect_jump_enabled=True, count=1)
        self.flow = ca.CooperativeLiveFlow(self.context, self.settings)
        patch.object(ca.time, "sleep").start()
        patch.object(ca, "discard_prearmed_backend").start()
        patch.object(ca, "append_current_run_event").start()
        patch.object(ca, "record_failure_reason").start()

    def test_life_zero_invokes_recovery_instead_of_manual_stop(self):
        self.flow.recover_failed_live_exit = Mock()
        with patch.object(ca.RealtimeProfilePlay, "run", return_value=False), \
             patch.object(ca, "current_live_run", return_value=NS(disconnect_jump_requested=True)):
            self.assertFalse(self.flow.play())
        self.flow.recover_failed_live_exit.assert_called_once()

    def test_disabled_option_never_changes_network(self):
        self.flow.settings["disconnect_jump_enabled"] = False
        self.flow.recover_failed_live_exit = Mock()
        with patch.object(ca.RealtimeProfilePlay, "run", return_value=False), \
             patch.object(ca, "current_live_run", return_value=NS(disconnect_jump_requested=True)):
            with self.assertRaises(ca.JumpOutUnavailable):
                self.flow.play()
        self.flow.recover_failed_live_exit.assert_not_called()

    def test_no_root_or_missing_popup_restarts_only_game(self):
        self.flow.disconnect_jump_out = Mock(return_value=False)
        self.flow.recover_failed_live_exit()
        self.controller.post_stop_app.assert_called_once_with(ca.GAME_PACKAGE)
        self.controller.post_start_app.assert_called_once_with(ca.GAME_PACKAGE)
        self.controller.post_shell.assert_not_called()

    def test_successful_disconnect_uses_home_verification_without_restart(self):
        self.flow.disconnect_jump_out = Mock(return_value=True)
        self.flow.recover_failed_live_exit()
        self.controller.post_stop_app.assert_not_called()

    def test_unconfirmed_network_restore_cannot_restart_or_reenter(self):
        self.flow.disconnect_jump_out = Mock(side_effect=ca.JumpOutUnavailable("网络恢复未确认"))
        with self.assertRaises(ca.JumpOutUnavailable):
            self.flow.recover_failed_live_exit()
        self.controller.post_stop_app.assert_not_called()
        self.controller.post_start_app.assert_not_called()

    def test_failed_stop_is_not_treated_as_restart_success(self):
        self.flow.disconnect_jump_out = Mock(return_value=False)
        self.controller.post_stop_app.return_value.wait.return_value = NS(succeeded=False)
        with self.assertRaises(ca.JumpOutUnavailable):
            self.flow.recover_failed_live_exit()
        self.controller.post_start_app.assert_not_called()

    def test_failed_round_is_retried_but_not_counted(self):
        self.flow.run_attempt = Mock(side_effect=[False, True])
        self.flow.recover_after_play_failure = Mock()
        self.flow.progress_callback = Mock()
        self.flow.navigate_completed_result = Mock(return_value=False)
        self.assertTrue(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_count, 2)
        self.flow.recover_after_play_failure.assert_called_once()
        self.flow.progress_callback.assert_called_once_with(1, 1)
        self.flow.navigate_completed_result.assert_called_once_with(replay=False)

    def test_consecutive_failures_stop_after_three_retries_even_when_infinite(self):
        self.flow.settings["count"] = 0
        self.flow.run_attempt = Mock(return_value=False)
        self.flow.recover_after_play_failure = Mock()
        self.assertFalse(self.flow.run())
        self.assertEqual(self.flow.run_attempt.call_count, 4)
        self.assertEqual(self.flow.recover_after_play_failure.call_count, 3)

    def test_shell_adapter_preserves_nonzero_device_exit(self):
        from realtime import team_recovery as tr
        with patch.object(tr, "shell_output", return_value="Permission denied\n__TEAM_SHELL_RC__1"):
            code, message = self.flow._adb_shell(("iptables", "-L"))
        self.assertEqual(code, 1)
        self.assertIn("Permission denied", message)

    def test_cleanup_retries_before_returning_failure(self):
        gate = Mock(last_error="root missing")
        gate.block.return_value = False
        gate.restore.side_effect = [False, False, True]
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            self.assertFalse(self.flow.disconnect_jump_out())
        self.assertEqual(gate.restore.call_count, 3)
        self.controller.post_click_key.assert_not_called()

    def test_cleanup_failure_is_fatal_after_bounded_retries(self):
        gate = Mock(last_error="root missing")
        gate.block.return_value = False
        gate.restore.return_value = False
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            with self.assertRaisesRegex(ca.JumpOutUnavailable, "网络恢复未确认"):
                self.flow.disconnect_jump_out()
        self.assertEqual(gate.restore.call_count, 3)

    def test_user_stop_during_disconnect_still_restores_network(self):
        gate = Mock()
        gate.block.return_value = True
        gate.restore.return_value = True
        self.flow._wait_and_click = Mock(side_effect=InterruptedError("用户已停止任务"))
        with patch.object(ca, "GameNetworkGate", return_value=gate):
            with self.assertRaises(InterruptedError):
                self.flow.disconnect_jump_out()
        gate.restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
