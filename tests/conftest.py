"""离线测试只注册 Python 类，不创建 Agent IPC 或连接模拟器。"""
from maa.agent.agent_server import AgentServer
from functools import wraps
from pathlib import Path
import sys

import pytest


def _offline_registration(_name):
    return lambda cls: cls


AgentServer.custom_action = staticmethod(_offline_registration)
AgentServer.custom_recognition = staticmethod(_offline_registration)


@pytest.fixture(autouse=True)
def optional_private_evidence(monkeypatch):
    """公开仓库不携带原始设备截图；只跳过实际需要缺失证据的测试。"""
    root = Path(__file__).resolve().parents[1]
    fixture_root = root / "tests" / "fixtures"
    recording_root = root / "docs" / "team-live-recording-review"
    for name, module in list(sys.modules.items()):
        reader = getattr(module, "imread_unicode", None) if name.startswith("test_") else None
        if reader is None:
            continue

        @wraps(reader)
        def read_optional(path, *args, _reader=reader, **kwargs):
            target = Path(path).resolve()
            private = target.is_relative_to(recording_root)
            if target.is_relative_to(fixture_root):
                group = target.relative_to(fixture_root).parts[0]
                private = group in {"team", "challenge", "formal-preflight"} or group.startswith("cooperative-")
            if private and not target.is_file():
                pytest.skip("需要本地原始录像/截图证据，公开仓库不包含该文件")
            return _reader(path, *args, **kwargs)

        monkeypatch.setattr(module, "imread_unicode", read_optional)
