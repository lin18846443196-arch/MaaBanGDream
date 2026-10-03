"""Actual Maa callback/reconnection replay; all device input is forbidden."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import maa
import numpy as np
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.custom_action import CustomAction

from check_cooperative_startup_resource import NoInputController
from test_team_live import ROOT
from realtime import cooperative_action as ca


class RootController(NoInputController):
    def __init__(self):
        self.rooted = False
        self.connections = 0
        self.commands = []
        super().__init__()

    def connect(self):
        self.connections += 1
        return True

    def shell(self, command, timeout):
        self.commands.append(command)
        if command.startswith("( id -u )"):
            output, code = ("0" if self.rooted else "2000"), 0
        elif command.startswith('( su -c "id -u" )'):
            output, code = "su: inaccessible or not found", 127
        elif command.startswith("( iptables -S OUTPUT )"):
            assert self.rooted
            output, code = "-P OUTPUT ACCEPT", 0
        elif command.startswith("( iptables -m owner -h )"):
            assert self.rooted
            output, code = "owner match options", 0
        elif command.startswith("( dumpsys package "):
            output, code = "userId=10058", 0
        else:
            raise AssertionError("Unexpected shell: " + command)
        return f"{output}\n__TEAM_SHELL_RC__{code}\n"


class RootProbe(CustomAction):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.result = None

    def run(self, context, argv):
        flow = ca.CooperativeLiveFlow(context, dict(ca.DEFAULT_SETTINGS, disconnect_jump_enabled=True))
        flow.navigate_to_cooperative_room_selection = Mock()
        flow.select_normal_room = Mock()
        def enable(controller, *, stopping):
            assert not stopping()
            self.controller.rooted = True
            return True, "root acquired"
        with patch.object(ca, "enable_adb_root", side_effect=enable):
            flow.enter_room()
        image = flow.capture_startup()
        assert image.shape == (720, 1280, 3)
        assert float(np.mean(image)) > 0
        assert self.controller.connections == 2
        assert not self.controller.input_attempts
        assert any('su -c "id -u"' in cmd for cmd in self.controller.commands)
        flow.select_normal_room.assert_called_once()
        self.result = dict(connections=self.controller.connections, root_verified=True,
                           frame_shape=list(image.shape), input_attempts=0)
        return True


def main():
    Library.open(Path(maa.__file__).parent / "bin", agent_server=False)
    output = ROOT / "temp/cooperative-auto-root-resource-check"
    output.mkdir(parents=True, exist_ok=True)
    Tasker.set_log_dir(output)
    resource = Resource()
    assert resource.post_bundle(ROOT / "resource").wait().succeeded
    controller = RootController()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    probe = RootProbe(controller)
    assert resource.register_custom_action("CooperativeRootProbe", probe)
    node = dict(recognition="DirectHit", action="Custom",
                custom_action="CooperativeRootProbe", pre_delay=0, post_delay=0,
                next=[], on_error=[])
    assert tasker.post_task("CooperativeRootProbe", {"CooperativeRootProbe": node}).wait().succeeded
    assert probe.result is not None
    (output / "results.json").write_text(json.dumps(probe.result, indent=2), encoding="utf-8")
    print(json.dumps(probe.result))
    print("Actual Maa reconnect, checked shell and capture passed; zero device inputs.")


if __name__ == "__main__":
    main()
