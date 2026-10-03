"""Actual Maa pipeline timing and short-transition replay, with no real device."""
import json
import time

import maa
import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from pathlib import Path

from test_team_live import ROOT
from realtime.cooperative_action import CooperativeLiveFlow, DEFAULT_SETTINGS


class NoInputController(CustomController):
    def __init__(self):
        self.started = None
        self.input_attempts = []
        super().__init__()

    def connect(self):
        return True

    def request_uuid(self):
        return "cooperative-startup-offline"

    def screencap(self):
        elapsed = -1 if self.started is None else time.monotonic() - self.started
        value = 0 if .15 <= elapsed <= .30 else 60
        return np.full((720, 1280, 3), value, np.uint8)

    def refuse(self, *args):
        self.input_attempts.append(args)
        raise AssertionError("Offline check forbids all device input/network changes")

    start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = shell = scroll = refuse


class Probe(CustomAction):
    def __init__(self, controller, *, old_capture=False):
        super().__init__()
        self.controller = controller
        self.old_capture = old_capture
        self.result = None

    def run(self, context, argv):
        flow = CooperativeLiveFlow(context, dict(DEFAULT_SETTINGS))
        flow.make_final_cover_entry_resolver = lambda: None
        flow.playfield_detector = lambda image: False
        flow.visible = lambda image, name, threshold: False
        if self.old_capture:
            flow.capture_startup = flow.capture
        def timeout():
            raise TimeoutError("missed transient black frame")
        flow.jump_after_download_timeout = timeout
        self.controller.started = time.monotonic()
        try:
            outcome = flow.watch_member_exit_before_black(timeout=.7)
        except TimeoutError:
            outcome = "timeout"
        self.result = {"old_capture": self.old_capture, "outcome": outcome,
                       "elapsed_ms": round((time.monotonic() - self.controller.started) * 1000, 2)}
        return True


def main():
    Library.open(Path(maa.__file__).parent / "bin", agent_server=False)
    output = ROOT / "temp/cooperative-startup-resource-check"
    output.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(output)
    resource = Resource()
    assert resource.post_bundle(ROOT / "resource").wait().succeeded
    controller = NoInputController()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    results = []
    for old in (True, False):
        probe = Probe(controller, old_capture=old)
        name = "StartupProbeOld" if old else "StartupProbeNew"
        assert resource.register_custom_action(name, probe)
        node = {"recognition": "DirectHit", "action": "Custom",
                "custom_action": name, "pre_delay": 0, "post_delay": 0,
                "next": [], "on_error": []}
        assert tasker.post_task(name, {name: node}).wait().succeeded
        assert probe.result is not None
        assert probe.result["outcome"] == ("timeout" if old else "black"), probe.result
        assert not controller.input_attempts, controller.input_attempts
        results.append(probe.result)
        print(json.dumps(probe.result), flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Actual Maa callback replay passed: short black frame captured; zero device inputs.", flush=True)


if __name__ == "__main__":
    main()
