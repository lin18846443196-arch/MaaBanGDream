"""Manual MuMu transition probe: switch apps and capture, without live play."""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from maa.controller import AdbController
from maa.library import Library
from maa.tasker import Tasker
from foreground_guard import GAME_PACKAGE, foreground_package
from realtime.cooperative_network import GameNetworkGate
from realtime.vision_io import imwrite_unicode


def main():
    device = json.loads((ROOT / "config/instances/default.json").read_text(encoding="utf-8"))["AdbDevice"]
    output = ROOT / "temp/cooperative-capture-transition-live"
    output.mkdir(parents=True, exist_ok=True)

    def shell(args):
        reply = subprocess.run([device["AdbPath"], "-s", device["AdbSerial"], "shell", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return reply.returncode, reply.stdout + reply.stderr

    network = GameNetworkGate(shell)
    assert network.check_available(), network.last_error
    assert network.restore_stale_escapes(), network.last_error
    Library.open(ROOT / "runtimes/win-x64/native", agent_server=False)
    Tasker.set_log_dir(output)
    controller = AdbController(device["AdbPath"], device["AdbSerial"],
        screencap_methods=64, input_methods=2, config=json.loads(device["Config"]),
        agent_path=ROOT / "libs/MaaAgentBinary")
    assert controller.post_connection().wait().succeeded
    context = NS(tasker=NS(controller=controller, stopping=False))
    results = []
    for cycle in range(3):
        if cycle:
            assert controller.post_click_key(3).wait().succeeded
            time.sleep(.6)
        assert controller.post_start_app(GAME_PACKAGE).wait().succeeded
        assert foreground_package(controller) == GAME_PACKAGE
        time.sleep(.5)
        for _ in range(20):
            image = controller.post_screencap().wait().get()
            if image is not None and float(np.std(image)) > 2.:
                break
            time.sleep(.5)
        for frame in range(3):
            before = time.monotonic()
            job = controller.post_screencap().wait()
            assert job.succeeded
            image = job.get()
            assert image.shape == (720, 1280, 3), image.shape
            assert float(np.std(image)) > 2., "blank frame"
            imwrite_unicode(output / f"cycle-{cycle}-frame-{frame}.png", image)
            results.append({"cycle": cycle, "frame": frame, "shape": list(image.shape),
                "capture_ms": round((time.monotonic() - before) * 1000, 2),
                "pixel_std": round(float(np.std(image)), 2), "foreground": GAME_PACKAGE})
            time.sleep(.15)
        print(json.dumps(results[-1]), flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("Three real MuMu app transitions and nine nonblank landscape captures passed.", flush=True)


if __name__ == "__main__":
    main()
