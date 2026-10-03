"""Actual Maa OCR on incident screenshots with a controller refusing all input."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import maa
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

from check_cooperative_startup_resource import NoInputController
from test_team_live import ROOT, Clock
from realtime import cooperative_action as ca
from realtime.vision_io import imread_unicode


def main():
    Library.open(Path(maa.__file__).parent / "bin", agent_server=False)
    output = ROOT / "temp/cooperative-restriction-resource-check"
    output.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(output)
    resource = Resource()
    assert resource.post_bundle(ROOT / "resource").wait().succeeded
    controller = NoInputController()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)

    def recognize(name, image):
        node = resource.get_node_object(name)
        result = tasker.post_recognition(
            node.recognition.type, node.recognition.param, image).wait().get()
        return result.nodes[-1].recognition

    context = NS(tasker=NS(stopping=False, controller=controller),
                 run_recognition=recognize)
    flow = ca.CooperativeLiveFlow(context, dict(ca.DEFAULT_SETTINGS))
    flow.click = Mock()  # record intended clicks without calling the controller
    clock = Clock()
    results = []
    fixtures = ROOT / "tests/fixtures/cooperative-20260930"
    with patch.object(ca.time, "monotonic", clock.time), patch.object(ca.time, "sleep", clock.advance):
        for name, seconds in (("restriction-24s", 24), ("restriction-second", 20)):
            image = imread_unicode(fixtures / (name + ".png"))
            recognition = recognize("CooperativeRestrictionCountdown", image)
            assert recognition.hit, recognition
            assert f"{seconds}秒" in recognition.best_result.text, recognition.best_result.text
            started = clock.now
            assert flow.wait_for_cooperative_restriction(image, remaining_budget=300)
            assert clock.now - started == seconds + 2
            results.append(dict(frame=name, text=recognition.best_result.text,
                                wait_seconds=clock.now - started))
        for name in ("live-select", "waiting-members", "notes-false-popup"):
            image = imread_unicode(fixtures / (name + ".png"))
            assert not flow.wait_for_cooperative_restriction(image, remaining_budget=300)
            results.append(dict(frame=name, restriction=False))
    assert flow.click.call_count == 2
    assert not controller.input_attempts
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("Actual Maa OCR and restriction handling passed; zero device inputs.")


if __name__ == "__main__":
    main()
