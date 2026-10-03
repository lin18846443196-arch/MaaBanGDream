"""A startup failure must keep evidence even before Play creates a recorder."""
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.vision_io import imread_unicode


class StartupEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.context = NS(tasker=NS(stopping=False, controller=Mock()))
        self.flow = ca.CooperativeLiveFlow(self.context, dict(ca.DEFAULT_SETTINGS))
        self.image = np.full((72, 128, 3), 160, np.uint8)
        self.run = NS(run_id="startup-current-round", song_title="test song",
                      song_level=26, difficulty="Expert", recording_path=None,
                      preparation_identity_image=self.image)

    def test_bounded_frames_and_preparation_are_kept_without_recording_path(self):
        frames = deque(((index / 60, self.image) for index in range(20)), maxlen=8)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(ca, "PROJECT_ROOT", Path(temporary)), \
             patch.object(ca, "current_live_run", return_value=self.run):
            self.flow._save_startup_failure_evidence(frames, "missed-transition")
            directory = Path(temporary) / "debug/cooperative-startup/startup-current-round"
            payload = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(payload["frames"]), 9)
            self.assertEqual(payload["frames"][0]["elapsed_seconds"], 12 / 60)
            self.assertEqual(payload["frames"][-1]["phase"], "preparation-identity")
            self.assertEqual(payload["song_level"], 26)
            for entry in payload["frames"]:
                self.assertIsNotNone(imread_unicode(directory / entry["screenshot"]))
        self.context.tasker.controller.post_click.assert_not_called()
        self.context.tasker.controller.post_click_key.assert_not_called()

    def test_disabled_diagnostics_and_user_stop_save_no_files(self):
        for stopped, diagnostic in ((True, True), (False, False)):
            with self.subTest(stopped=stopped, diagnostic=diagnostic), \
                 tempfile.TemporaryDirectory() as temporary, \
                 patch.object(ca, "PROJECT_ROOT", Path(temporary)), \
                 patch.object(ca, "current_live_run", return_value=self.run):
                self.context.tasker.stopping = stopped
                self.flow.settings["diagnostic_trace"] = diagnostic
                self.flow._save_startup_failure_evidence([(0, self.image)], "timeout")
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_evidence_write_error_does_not_replace_the_original_failure(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(ca, "PROJECT_ROOT", Path(temporary)), \
             patch.object(ca, "current_live_run", return_value=self.run), \
             patch.object(ca, "imwrite_unicode", side_effect=OSError("full disk")):
            self.flow._save_startup_failure_evidence([(0, self.image)], "missed-transition")
        self.context.tasker.controller.post_click.assert_not_called()

    def test_transition_timeout_keeps_last_eight_frames_before_recovery(self):
        clock = Clock()
        self.flow.make_final_cover_entry_resolver = Mock(return_value=None)
        self.flow.visible = Mock(return_value=False)
        self.flow.playfield_detector = Mock(return_value=False)
        self.flow._save_startup_failure_evidence = Mock()
        self.flow.jump_after_download_timeout = Mock(side_effect=ca.CooperativeStartupRetry("timeout"))

        def capture():
            clock.advance(.01)
            return self.image

        self.flow.capture_startup = capture
        with patch.object(ca.time, "monotonic", clock.time), \
             patch.object(ca.time, "sleep", clock.advance):
            with self.assertRaises(ca.CooperativeStartupRetry):
                self.flow.watch_member_exit_before_black(timeout=.5)
        frames, status = self.flow._save_startup_failure_evidence.call_args.args
        self.assertEqual(status, "transition-timeout")
        self.assertEqual(len(frames), 8)
        self.assertLess(frames[0][0], frames[-1][0])
        self.flow.jump_after_download_timeout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
