"""Regression for Maa's null parameters on legacy solo/challenge entry nodes."""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace as NS
from unittest.mock import ANY, Mock, call, patch

import numpy as np

# Reuse the offline registration harness; importing the real server without it
# would start AgentServer IPC. No device or native input is used by these tests.
from test_team_live import ROOT, fp


class FormalPreflightParameterTests(unittest.TestCase):
    def setUp(self):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.controller = Mock()
        self.controller.post_screencap.return_value.wait.return_value.get.return_value = self.image
        self.context = NS(tasker=NS(stopping=False, controller=self.controller),
                          run_recognition=Mock(return_value=NS(hit=False)))
        self.action = fp.RealtimeFormalPreflight()
        self.mode = self.enterContext(patch.object(fp, 'formal_live_mode_is_off', return_value=True))
        self.cutin = self.enterContext(patch.object(fp, 'cut_in_is_checked', return_value=False))
        self.guard = self.enterContext(patch.object(fp, 'require_game_foreground'))
        self.refresh = self.enterContext(patch.object(fp, 'capture_image', return_value=self.image))
        self.enterContext(patch.object(fp, '_wait', return_value=True))

    def assert_default_checks(self, args):
        for mock in (self.controller, self.context.run_recognition, self.mode, self.cutin, self.refresh):
            mock.reset_mock()
        self.assertTrue(self.action.run(self.context, args))
        self.controller.post_screencap.assert_called_once_with()
        self.context.run_recognition.assert_called_once_with('AutoLiveEnabled', ANY)
        self.mode.assert_called_once_with(self.image)
        self.cutin.assert_called_once_with(self.image)
        self.refresh.assert_not_called()
        self.controller.post_click.assert_not_called()

    def test_missing_field_keeps_all_default_checks(self):
        self.assert_default_checks(NS())

    def test_all_legacy_empty_values_keep_all_default_checks(self):
        for raw in (None, '', '  ', 'null', ' null ', '{}', {}):
            with self.subTest(raw=raw):
                self.assert_default_checks(NS(custom_action_param=raw))

    def test_actual_solo_keeps_legacy_parameters_and_challenge_guards_its_page(self):
        solo = json.loads((ROOT/'resource/pipeline/realtime_multi_live.json').read_text(encoding='utf-8'))
        challenge = json.loads((ROOT/'resource/pipeline/challenge_live.json').read_text(encoding='utf-8'))
        self.assertEqual(solo['RealtimeLiveFormalReady']['custom_action'], 'RealtimeFormalPreflight')
        self.assertNotIn('custom_action_param', solo['RealtimeLiveFormalReady'])
        self.assertEqual(challenge['ChallengeBandMarker']['custom_action'], 'RealtimeFormalPreflight')
        self.assertEqual(challenge['ChallengeBandMarker']['custom_action_param'], {
            'page_guard': 'ChallengeBandMarker', 'refresh_node': 'CommonRefreshScreen',
        })
        raw = json.dumps(solo['RealtimeLiveFormalReady'].get('custom_action_param'))
        self.assertEqual(raw, 'null')
        self.assert_default_checks(NS(custom_action_param=raw))
        self.context.run_recognition.side_effect = lambda node, image: NS(
            hit=node == 'ChallengeBandMarker', box=None)
        self.assertTrue(self.action.run(self.context, NS(custom_action_param=json.dumps(
            challenge['ChallengeBandMarker']['custom_action_param']))))
        self.refresh.assert_called_once_with(self.context, node='CommonRefreshScreen')

    def test_null_still_disables_auto_mode_and_cutin_in_order(self):
        self.context.run_recognition.side_effect = [NS(hit=True, box=NS(x=10,y=20,w=30,h=40))] + [NS(hit=False)] * 3
        self.mode.side_effect = [False, True, True]
        self.cutin.side_effect = [True, False]
        self.assertTrue(self.action.run(self.context, NS(custom_action_param='null')))
        self.assertEqual(self.controller.post_click.call_args_list,
                         [call(25, 40), call(*fp.MODE_TOGGLE_POINT), call(500, 650)])
        self.assertEqual(self.guard.call_count, 3)
        self.assertEqual(self.controller.post_screencap.call_count, 4)

    def test_null_keeps_foreground_guard_before_input(self):
        self.cutin.return_value = True
        self.guard.side_effect = RuntimeError('foreground mismatch')
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertFalse(self.action.run(self.context, NS(custom_action_param='null')))
        self.guard.assert_called_once_with(self.controller)
        self.controller.post_click.assert_not_called()

    def test_null_respects_stop_before_capture(self):
        self.context.tasker.stopping = True
        self.assertTrue(self.action.run(self.context, NS(custom_action_param='null')))
        self.controller.post_screencap.assert_not_called()
        self.controller.post_click.assert_not_called()

    def test_invalid_types_and_malformed_json_fail_before_input(self):
        for raw in ('[]', '[1]', '0', 'false', '"null"', '{', [], False, 0):
            with self.subTest(raw=raw), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertFalse(self.action.run(self.context, NS(custom_action_param=raw)))
        self.controller.post_screencap.assert_not_called()
        self.controller.post_click.assert_not_called()
        self.refresh.assert_not_called()

    def test_team_object_preserves_guard_and_fast_refresh(self):
        params = dict(page_guard='TeamPrepareTitle', cut_in_off_node='TeamCutInOff',
                      refresh_node='TeamRefreshScreen', check_auto_live=False,
                      check_performance_mode=False)
        self.context.run_recognition.return_value = NS(hit=True)
        self.assertTrue(self.action.run(self.context, NS(custom_action_param=json.dumps(params))))
        self.refresh.assert_called_once_with(self.context, node='TeamRefreshScreen')
        self.assertEqual([c.args[0] for c in self.context.run_recognition.call_args_list],
                         ['TeamPrepareTitle', 'TeamCutInOff'])
        self.mode.assert_not_called()
        self.controller.post_screencap.assert_not_called()
        self.controller.post_click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
