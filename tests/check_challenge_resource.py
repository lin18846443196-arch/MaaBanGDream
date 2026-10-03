"""Replay challenge point selection through native Maa without a device."""
import json
from pathlib import Path
from unittest.mock import patch

import maa
from maa.controller import CustomController
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

from test_team_live import ROOT, REGISTERED
from realtime import challenge_points as cp
from realtime.vision_io import imread_unicode


FIXTURES = ROOT / "tests" / "fixtures" / "challenge"


def frame(name):
    image = imread_unicode(FIXTURES / f"{name}.png")
    assert image is not None and image.shape == (720, 1280, 3), name
    return image


class ReplayController(CustomController):
    def __init__(self, frames):
        self.frames = frames
        self.page = "points-one"
        self.clicks = []
        self.refused = []
        super().__init__()

    def connect(self):
        return True

    def request_uuid(self):
        return "challenge-offline-replay"

    def get_features(self):
        return 0

    def screencap(self):
        return self.frames[self.page].copy()

    def click(self, x, y):
        point = (x, y)
        transitions = {
            ("points-one", cp.MULTIPLIER_TARGETS[8]): "points-eight",
            ("points-eight", cp.CONFIRM_POINT): "band-ready",
            ("points-one", cp.CANCEL_POINT): "song-select",
        }
        destination = transitions.get((self.page, point))
        if destination is None:
            return self.refuse("click", self.page, point)
        self.clicks.append(point)
        self.page = destination
        return True

    def refuse(self, *args):
        self.refused.append(args)
        raise AssertionError(f"Offline replay rejected input: {args}")

    start_app = stop_app = swipe = touch_down = touch_move = touch_up = refuse
    click_key = input_text = key_down = key_up = shell = scroll = relative_move = refuse

    def reset(self):
        self.page = "points-one"
        self.clicks.clear()
        self.refused.clear()


class TracedSelect(cp.ChallengePointsSelect):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, context, argv):
        result = super().run(context, argv)
        self.calls.append(dict(node=argv.node_name, result=result))
        return result


class TracedConfirm(cp.ChallengePointsConfirm):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, context, argv):
        result = super().run(context, argv)
        self.calls.append(dict(node=argv.node_name, result=result))
        return result


def terminal(recognition="DirectHit"):
    return dict(recognition=recognition, action="DoNothing", next=[], on_error=[],
                pre_delay=0, post_delay=0, timeout=1500)


def main():
    Library.open(Path(maa.__file__).parent / "bin", agent_server=False)
    output = ROOT / "temp" / "challenge-resource-check"
    output.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(output)
    frames = {name: frame(name) for name in
              ("song-select", "points-one", "points-eight", "band-ready")}
    resource = Resource()
    assert resource.post_bundle(ROOT / "resource").wait().succeeded
    assert REGISTERED["ChallengePointsSelect"] is cp.ChallengePointsSelect
    assert REGISTERED["ChallengePointsConfirm"] is cp.ChallengePointsConfirm
    assert REGISTERED["ChallengeSongPageVisible"] is cp.ChallengeSongPageVisible
    select, confirm = TracedSelect(), TracedConfirm()
    assert resource.register_custom_action("ChallengePointsSelect", select)
    assert resource.register_custom_action("ChallengePointsConfirm", confirm)
    assert resource.register_custom_recognition("ChallengeSongPageVisible", cp.ChallengeSongPageVisible())
    controller = ReplayController(frames)
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    results = []

    checks = (
        ("ChallengeSongMarker", "song-select", True),
        ("ChallengeSongMarker", "band-ready", False),
        ("ChallengeSongMarker", "points-one", False),
        ("ChallengeSongMarker", "points-eight", False),
        ("ChallengePrepare", "song-select", True),
        ("ChallengePrepare", "points-one", False),
        ("ChallengePrepare", "points-eight", False),
        ("ChallengePrepare", "band-ready", False),
        ("ChallengePointMarker", "points-one", True),
        ("ChallengePointMarker", "points-eight", True),
        ("ChallengePointMarker", "song-select", False),
        ("ChallengePointMarker", "band-ready", False),
        ("ChallengeBandMarker", "band-ready", True),
        ("ChallengeBandMarker", "song-select", False),
        ("ChallengeBandMarker", "points-one", False),
        ("ChallengeBandMarker", "points-eight", False),
    )
    for name, page, expected in checks:
        node = resource.get_node_object(name)
        detail = tasker.post_recognition(
            node.recognition.type, node.recognition.param, frames[page]
        ).wait().get()
        assert detail and detail.nodes, (name, page, detail)
        hit = bool(detail.nodes[-1].recognition.hit)
        assert hit == expected, (name, page, expected, hit)
        results.append(dict(check="recognition", node=name, page=page,
                            hit=hit, expected=expected))
    print(f"Native challenge template checks passed: {len(checks)}", flush=True)

    expected_targets = {"Easy": [709, 545], "Normal": [822, 545], "Hard": [934, 545],
                        "Expert": [1048, 545], "Special": [1180, 545]}
    interface = json.loads((ROOT / "interface.json").read_text(encoding="utf-8"))
    cases = interface["option"]["ChallengeDifficulty"]["cases"]
    assert [case["name"] for case in cases] == list(expected_targets)
    for case in [None, *cases]:
        difficulty = "Easy" if case is None else case["name"]
        if case is not None:
            assert resource.override_pipeline(case["pipeline_override"])
        params = resource.get_node_object("ChallengeDifficulty").action.param.custom_action_param
        assert params["difficulty"] == difficulty
        assert params["mode"] == "challenge"
        assert params["page_guard"] == "ChallengeSongMarker"
        assert params["refresh_node"] == "CommonRefreshScreen"
        assert params["song_title_roi"] == [200, 327, 368, 38]
        assert params["difficulty_targets"] == expected_targets
        for name in ("ChallengeSpeedSettingsGate", "ChallengeProfileCheck", "ChallengeSettingsGate"):
            gate = resource.get_node_object(name).action.param.custom_action_param
            assert gate["difficulty"] == difficulty, (name, gate)
            assert gate["run_mode"] == "challenge", (name, gate)
        settings = resource.get_node_object("ChallengeSettingsGate").action.param.custom_action_param
        assert settings["confirm_preparation_identity"] is True
        assert settings["refresh_node"] == "CommonRefreshScreen"
        play = "ChallengePlay" + ("" if difficulty == "Easy" else difficulty)
        assert [node.name for node in resource.get_node_object("ChallengeStart").next] == [play]
        play_params = resource.get_node_object(play).action.param.custom_action_param
        assert play_params["difficulty"] == difficulty
        assert play_params["run_mode"] == "challenge"
        assert play_params["settings_gate_required"] is True
        results.append(dict(check="difficulty-options", case="base" if case is None else difficulty,
                            play=play, passed=True))
    print("Native default and five difficulty overrides passed.", flush=True)

    overrides = {
        "ChallengePointSelect": dict(pre_delay=0, post_delay=0, on_error=[]),
        "ChallengePointConfirm": dict(pre_delay=0, post_delay=0, on_error=[]),
        "ChallengeBandMarker": terminal("TemplateMatch"),
        "ChallengePointsExhaustedRecover": terminal(),
    }

    real_ocr = object()

    def replay(name, readings=real_ocr):
        controller.reset()
        select.calls.clear()
        confirm.calls.clear()
        with patch.object(cp, "require_game_foreground"), patch.object(cp, "evidence"):
            if readings is real_ocr:
                job = tasker.post_task("ChallengePointSelect", overrides).wait()
            else:
                with patch.object(cp, "read_points", return_value=readings):
                    job = tasker.post_task("ChallengePointSelect", overrides).wait()
        detail = job.get()
        nodes = [node.name for node in detail.nodes]
        assert not controller.refused, controller.refused
        results.append(dict(check=name, succeeded=job.succeeded, nodes=nodes,
                            clicks=controller.clicks.copy(), select=select.calls.copy(),
                            confirm=confirm.calls.copy()))
        return job, nodes

    job, nodes = replay("recorded-eight-times")
    assert job.succeeded, results[-1]
    assert controller.clicks == [cp.MULTIPLIER_TARGETS[8], cp.CONFIRM_POINT], controller.clicks
    assert controller.page == "band-ready"
    assert select.calls == [dict(node="ChallengePointSelect", result=True)]
    assert confirm.calls == [dict(node="ChallengePointConfirm", result=True)]
    assert "ChallengeBandMarker" in nodes and "ChallengeStart" not in nodes
    print("Recorded 15830 points: selected 8x and confirmed with exactly two clicks.", flush=True)

    job, nodes = replay("points-exhausted", 199)
    assert job.succeeded, results[-1]
    assert controller.clicks == [cp.CANCEL_POINT], controller.clicks
    assert controller.page == "song-select"
    assert "ChallengePointsExhaustedRecover" in nodes
    assert not confirm.calls
    assert "ChallengeBandMarker" not in nodes and "ChallengeStart" not in nodes
    print("199 points: cancelled once and reached exhaustion branch.", flush=True)

    job, nodes = replay("ocr-unreadable", None)
    assert job.failed, results[-1]
    assert not controller.clicks and not controller.refused
    assert select.calls == [dict(node="ChallengePointSelect", result=False)]
    assert not confirm.calls
    assert "ChallengeBandMarker" not in nodes and "ChallengeStart" not in nodes
    print("Unreadable OCR: native task failed without input.", flush=True)

    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Offline native Maa challenge resource replay passed.", flush=True)


if __name__ == "__main__":
    main()
