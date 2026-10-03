"""Replay solo and guarded challenge preflight via real callbacks, without ADB."""
import json
from pathlib import Path

import maa
from maa.controller import CustomController
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

# Import actions through the existing offline registration harness.
from test_team_live import ROOT, fp
from realtime.vision_io import imread_unicode


class NoInputController(CustomController):
    def __init__(self, image):
        self.image = image
        self.input_attempts = []
        super().__init__()

    def connect(self):
        return True

    def request_uuid(self):
        return 'formal-preflight-offline-replay'

    def screencap(self):
        return self.image.copy()

    def refuse(self, *args):
        self.input_attempts.append(args)
        raise AssertionError('Offline replay forbids all device input and shell commands')

    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = shell = scroll = refuse


class TracedPreflight(fp.RealtimeFormalPreflight):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, context, argv):
        self.calls.append(argv.custom_action_param)
        return super().run(context, argv)


def main():
    # maa.agent selects its reverse DLL at import. Use standalone MaaFramework
    # for actual Resource/Tasker objects in this isolated test process.
    Library.open(Path(maa.__file__).parent/'bin', agent_server=False)
    output = ROOT/'temp/formal-preflight-resource-check'
    output.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(output)
    image = imread_unicode(ROOT/'tests/fixtures/formal-preflight/solo-ready-20260928.png')
    assert image is not None and image.shape == (720, 1280, 3)
    assert fp.formal_live_mode_is_off(image)
    assert not fp.cut_in_is_checked(image)
    resource = Resource()
    assert resource.post_bundle(ROOT/'resource').wait().succeeded
    action = TracedPreflight()
    assert resource.register_custom_action('RealtimeFormalPreflight', action)
    controller = NoInputController(image)
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)

    def recognize(name):
        node = resource.get_node_object(name)
        detail = tasker.post_recognition(node.recognition.type, node.recognition.param, image).wait().get()
        assert detail and detail.nodes, (name, detail)
        return detail.nodes[-1].recognition.hit

    assert recognize('RealtimeLiveFormalReady'), 'Actual solo-ready template must match the incident image'
    assert not recognize('AutoLiveEnabled'), 'Auto Live is OFF in the incident image'
    results = []
    for node in ('RealtimeLiveFormalReady', 'ChallengeBandMarker'):
        # End at preflight; never launch Native, recover Home, or start a song.
        override = dict(next=[], on_error=[], pre_delay=0, post_delay=0, timeout=1500)
        if node == 'ChallengeBandMarker':
            controller.image = imread_unicode(ROOT/'tests/fixtures/challenge/band-ready.png')
            assert controller.image is not None
            override['recognition'] = 'TemplateMatch'
        assert tasker.post_task(node, {node: override}).wait().succeeded, node
        if node == 'ChallengeBandMarker':
            assert json.loads(action.calls[-1]) == {
                'page_guard': 'ChallengeBandMarker', 'refresh_node': 'CommonRefreshScreen',
            }, action.calls
        else:
            assert action.calls[-1] == 'null', (node, action.calls)
        assert not controller.input_attempts, controller.input_attempts
        results.append(dict(node=node, received_parameters=action.calls[-1],
                            actual_page_match=True, passed=True))
        print(f'{node}: actual Maa callback; preflight passed; no input', flush=True)
    assert len(action.calls) == 2, action.calls
    (output/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Offline native Maa callback replay passed for solo and challenge.', flush=True)


if __name__ == '__main__':
    main()
