"""Replay the October 1 connection modal without emulator or network input."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import cv2
import numpy as np

from test_team_live import ROOT, Clock
import common_recover as cr
from realtime.vision_io import imread_unicode
from realtime import live_session as ls


NODES = json.loads((ROOT / 'resource/pipeline/cooperative_live.json').read_text(encoding='utf-8'))
RECOVERY = dict(NODES['CooperativeRecover']['custom_action_param'])
POPUP = ROOT / 'tests/fixtures/cooperative-2009/home-connect-failed.png'
NEGATIVES = (
    'team/home-circle.png',
    'cooperative-20260930/live-select.png',
    'cooperative-replay-20261001/room-selection.png',
    'cooperative-replay-20261001/activity-points.png',
    'cooperative-20260930/restriction-second.png',
    'cooperative-20261001-popup/continue.png',
    'cooperative-20261001-popup/confirm-current.png',
)


def recognize_retry(name, image):
    """Use the real configured templates; simulate only the Maa wrapper."""
    node = NODES[name]
    if node['recognition'] == 'And':
        children = [recognize_retry(child, image) for child in node['all_of']]
        return NS(hit=all(child.hit for child in children), box=children[node['box_index']].box)
    x, y, width, height = node['roi']
    target = image[y:y + height, x:x + width]
    template = imread_unicode(ROOT / 'resource/image' / node['template'])
    _, score, _, position = cv2.minMaxLoc(cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED))
    box = NS(x=x + position[0], y=y + position[1], w=template.shape[1], h=template.shape[0])
    return NS(hit=score >= node['threshold'], box=box)


class ConnectionModalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch.object(ls, '_CURRENT_LIVE_RUN', None)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.clock = Clock()
        self.page = 'modal'
        self.popup = imread_unicode(POPUP)
        self.home = imread_unicode(ROOT / 'tests/fixtures/team/home-circle.png')
        self.clicked = []
        self.recognized = []
        self.controller = Mock()
        self.context = NS(tasker=NS(stopping=False, controller=self.controller), run_recognition=self.recognize)
        self.controller.post_click.side_effect = self.click

    def click(self, x, y):
        self.clicked.append(((x, y), self.clock.now))
        self.page = 'home'
        return Mock()

    def capture(self, *_args, **_kwargs):
        return self.home if self.page == 'home' else self.popup

    def recognize(self, name, image):
        self.recognized.append(name)
        if name.startswith('CooperativeConnectFailed'):
            return recognize_retry(name, image)
        # Reproduce the bug: Home and cancel can both match behind the modal.
        return NS(hit=name == 'CooperativeHomeMarker' or
                  (name == 'QuitConfirmCancel' and self.page == 'modal'),
                  box=NS(x=415, y=495, w=185, h=55))

    def run_recovery(self, params=None):
        values = {**RECOVERY, 'restart_limit': 2, 'escape_timeout_ms': 10000, **(params or {})}
        def wait(context, seconds):
            self.clock.advance(seconds)
            return not context.tasker.stopping
        with patch.object(cr, '_prepare_game', return_value=(True, False)), \
             patch.object(cr, 'require_game_foreground'), \
             patch.object(cr, 'capture_image', side_effect=self.capture), \
             patch.object(cr, '_wait_unless_stopping', side_effect=wait), \
             patch.object(cr.time, 'monotonic', self.clock.time), \
             patch.object(cr, 'record_failure_reason') as reason:
            result = cr.CommonRecover().run(self.context, NS(custom_action_param=values))
        return result, reason

    def test_connection_modal_precedes_home_and_cancel(self):
        # The unrelated cancel template only appears while the modal is up.
        original = self.recognize
        def recognize(name, image):
            if name == 'QuitConfirmCancel' and self.page == 'home':
                return NS(hit=False, box=None)
            return original(name, image)
        self.context.run_recognition = recognize
        result, reason = self.run_recovery()
        self.assertTrue(result)
        self.assertEqual(self.clicked, [((769, 526), 0.0)])
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertEqual(self.recognized[0], 'CooperativeConnectFailedRetry')
        self.controller.post_click_key.assert_not_called()
        self.controller.post_stop_app.assert_not_called()
        self.controller.post_start_app.assert_not_called()
        reason.assert_not_called()

    def test_persistent_connection_failure_stops_after_five_retries(self):
        def click(x, y):
            self.clicked.append(((x, y), self.clock.now))
            return Mock()
        self.controller.post_click.side_effect = click
        result, reason = self.run_recovery()
        self.assertFalse(result)
        self.assertEqual([point for point, _ in self.clicked], [(769, 526)] * 5)
        self.assertGreaterEqual(self.clock.now, 5.0)
        self.assertIn('连接失败', reason.call_args.args[0])
        self.assertIn('5 次', reason.call_args.args[0])
        self.assertNotIn('CooperativeHomeMarker', self.recognized)
        self.controller.post_click_key.assert_not_called()
        self.controller.post_stop_app.assert_not_called()
        self.controller.post_start_app.assert_not_called()

    def test_body_without_retry_button_blocks_background_home_until_timeout(self):
        self.popup[500:553, 660:878] = 255
        result, reason = self.run_recovery()
        self.assertFalse(result)
        self.assertIn('连接失败', reason.call_args.args[0])
        self.assertFalse(self.clicked)
        self.assertNotIn('CooperativeHomeMarker', self.recognized)
        self.controller.post_click_key.assert_not_called()
        self.controller.post_stop_app.assert_not_called()
        self.controller.post_start_app.assert_not_called()

    def test_dimmed_modal_does_not_receive_retry_click(self):
        self.popup = np.round(self.popup.astype(float) * .65).astype(np.uint8)
        result, reason = self.run_recovery()
        self.assertFalse(result)
        self.assertFalse(self.clicked)
        self.assertIn('连接失败', reason.call_args.args[0])
        self.controller.post_click_key.assert_not_called()
        self.controller.post_stop_app.assert_not_called()

    def test_stop_after_retry_click_returns_cancelled_without_more_input(self):
        def click(x, y):
            self.clicked.append(((x, y), self.clock.now))
            self.context.tasker.stopping = True
            return Mock()
        self.controller.post_click.side_effect = click
        result, reason = self.run_recovery()
        self.assertTrue(result)
        self.assertEqual(len(self.clicked), 1)
        self.controller.post_click_key.assert_not_called()
        reason.assert_not_called()

    def test_stop_during_presence_check_never_reports_failure(self):
        def recognize(name, image):
            result = self.recognize(name, image)
            self.context.tasker.stopping = True
            return result
        self.context.run_recognition = recognize
        result, reason = self.run_recovery()
        self.assertTrue(result)
        self.assertFalse(self.clicked)
        reason.assert_not_called()

    def test_retry_interval_is_at_least_one_second(self):
        self.controller.post_click.side_effect = lambda *point: (self.clicked.append((point, self.clock.now)) or Mock())
        result, _ = self.run_recovery({'modal_retry_interval_ms': 10})
        self.assertFalse(result)
        times = [stamp for _, stamp in self.clicked]
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(times, times[1:])))

    def test_unconfigured_tasks_preserve_existing_home_recovery(self):
        self.context.run_recognition = lambda name, image: NS(hit=name == 'CooperativeHomeMarker', box=None)
        result, reason = self.run_recovery({'modal_retry_nodes': [], 'modal_retry_presence_nodes': []})
        self.assertTrue(result)
        self.assertFalse(self.clicked)
        self.assertEqual(self.clock.now, 0)
        reason.assert_not_called()

    def recording(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ls.reset_live_run(mode='cooperative', difficulty='Expert', debug_recording=True)
        ls.update_live_run(recording_path=directory.name)
        return Path(directory.name)

    def test_retry_evidence_is_linked_to_current_recording(self):
        recording = self.recording()
        result, _ = self.run_recovery()
        self.assertTrue(result)
        images = list((recording / 'navigation').glob('*.png'))
        self.assertEqual(len(images), 1)
        events = [json.loads(line) for line in (recording / 'lifecycle.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['phase'], 'common-recover-connection')
        self.assertEqual(events[0]['status'], 'retry')
        self.assertEqual(events[0]['details']['attempt'], 1)
        self.assertEqual(recording / events[0]['details']['screenshot'], images[0])

    def test_missing_retry_button_evidence_is_bounded_to_first_and_timeout(self):
        recording = self.recording()
        self.popup[500:553, 660:878] = 255
        result, _ = self.run_recovery()
        self.assertFalse(result)
        self.assertEqual(len(list((recording / 'navigation').glob('*.png'))), 2)
        events = [json.loads(line) for line in (recording / 'lifecycle.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['status'] for event in events], ['retry-button-unrecognized', 'timeout'])

    def test_exhausted_evidence_records_five_attempts_and_final_failure(self):
        recording = self.recording()
        self.controller.post_click.side_effect = lambda *point: (self.clicked.append((point, self.clock.now)) or Mock())
        result, _ = self.run_recovery({'modal_retry_limit': 100})
        self.assertFalse(result)
        self.assertEqual(len(list((recording / 'navigation').glob('*.png'))), 6)
        events = [json.loads(line) for line in (recording / 'lifecycle.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([event['status'] for event in events], ['retry'] * 5 + ['exhausted'])
        self.assertEqual(events[-1]['details']['attempts'], 5)

    def test_evidence_write_exception_does_not_block_recovery(self):
        self.recording()
        with patch.object(cr, 'imwrite_unicode', side_effect=OSError('recording disk unavailable')):
            result, reason = self.run_recovery()
        self.assertTrue(result)
        self.assertEqual(len(self.clicked), 1)
        reason.assert_not_called()

    def test_evidence_disabled_or_without_current_recording_does_not_write(self):
        with patch.object(cr, 'imwrite_unicode') as write:
            result, _ = self.run_recovery()
        self.assertTrue(result)
        write.assert_not_called()
        self.recording()
        self.page = 'modal'
        with patch.object(cr, 'imwrite_unicode') as write:
            result, _ = self.run_recovery({'modal_retry_evidence_enabled': False})
        self.assertTrue(result)
        write.assert_not_called()


class ConnectionModalResourceTests(unittest.TestCase):
    def test_startup_and_finalize_enable_the_same_combined_guard(self):
        for node in ('CooperativeRecover', 'CooperativeReturnHome'):
            params = NODES[node]['custom_action_param']
            self.assertEqual(params['modal_retry_nodes'], ['CooperativeConnectFailedRetry'])
            self.assertEqual(params['modal_retry_presence_nodes'], ['CooperativeConnectFailedBody'])
            self.assertEqual(params['modal_retry_limit'], 5)
            self.assertGreaterEqual(params['modal_retry_interval_ms'], 1000)
            self.assertEqual(params['modal_retry_min_brightness'], 220)
            self.assertTrue(params['modal_retry_evidence_enabled'])

    def test_button_with_missing_body_does_not_pass_combined_guard(self):
        image = imread_unicode(POPUP)
        image[345:370, 580:685] = 255
        self.assertTrue(recognize_retry('CooperativeConnectFailedRetryButton', image).hit)
        self.assertFalse(recognize_retry('CooperativeConnectFailedRetry', image).hit)

    @unittest.skipUnless(POPUP.is_file(), '需要本地连接失败截图，公开仓库不包含原始截图')
    def test_real_maa_combined_recognition_and_negative_pages(self):
        # A separate process owns the real framework DLL; importing the unit
        # test agent selects its reverse-proxy DLL in this process.
        script = r'''
import json
from pathlib import Path
import cv2
import numpy as np
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker
root = Path.cwd()
Tasker.set_log_dir(root / 'temp/cooperative-2009-resource-check')
class NoInput(CustomController):
    def connect(self): return True
    def request_uuid(self): return 'connection-modal-offline'
    def screencap(self): return np.zeros((720,1280,3),np.uint8)
    def refuse(self,*args): raise AssertionError('No emulator inputs permitted')
    start_app=stop_app=click=swipe=touch_down=touch_move=touch_up=click_key=input_text=key_down=key_up=refuse
resource=Resource()
assert resource.post_bundle(root / 'resource').wait().succeeded
controller=NoInput()
assert controller.post_connection().wait().succeeded
tasker=Tasker()
assert tasker.bind(resource,controller)
node=resource.get_node_object('CooperativeConnectFailedRetry')
def hit(image):
    detail=tasker.post_recognition(node.recognition.type,node.recognition.param,image).wait().get()
    return detail.nodes[-1].recognition
image=cv2.imread(str(root/'tests/fixtures/cooperative-2009/home-connect-failed.png'))
result=hit(image)
assert result.hit,result
assert tuple((result.box.x,result.box.y,result.box.w,result.box.h)) == (660,500,218,53),result.box
image[345:370,580:685]=255
assert not hit(image).hit
negative_paths=json.loads(__import__('sys').argv[1])
for relative in negative_paths:
    image=cv2.imread(str(root/'tests/fixtures'/relative))
    assert not hit(image).hit,relative
print('Maa And recognition passed positive, missing-body and 7 negative pages.')
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, json.dumps(NEGATIVES)],
                                cwd=ROOT, text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('recognition passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
