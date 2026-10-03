"""默认校准只补缺失文件，两个引擎按环境选择，用户选择保持原样。"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from profile_defaults import seed_default_profiles
from realtime.profile_store import EnvironmentSignature, RealtimeProfileStore


def test_fresh_defaults_resolve_expert_for_both_engines(tmp_path):
    seeds = ROOT / "packaging/profiles"
    target = tmp_path / "profiles"
    assert len(seed_default_profiles(seeds, target)) == 3
    store = RealtimeProfileStore(target)
    assert store.pinned_profile("Expert") is None
    for engine, expected in (("legacy", "expert-20260905233716.json"),
                             ("native", "expert-20261003194036.json")):
        signature = EnvironmentSignature((1280, 720), 240, 60, "standard", 5.0, engine=engine)
        selected = store.resolve_latest(difficulty="Expert", current_signature=signature)
        assert selected.profile_path.name == expected
        assert selected.timing_offset_ms == 60
    assert seed_default_profiles(seeds, target) == []


def test_upgrade_adds_native_without_overwriting_existing_profile_or_selection(tmp_path):
    target = tmp_path / "profiles"
    target.mkdir()
    old_profile = target / "expert-20260905233716.json"
    old_profile.write_bytes(b'{"custom": "keep user calibration"}')
    selection = target / "selection.json"
    selection.write_bytes(b'{"version":1,"pinned":{"Expert":"my-profile.json"}}')
    existing = {path.name: path.read_bytes() for path in target.iterdir()}
    assert seed_default_profiles(ROOT / "packaging/profiles", target) == ["expert-20261003194036.json"]
    assert all((target / name).read_bytes() == data for name, data in existing.items())
    native = json.loads((target / "expert-20261003194036.json").read_text(encoding="utf-8"))
    assert native["accepted"] is True
    assert native["environment"]["engine"] == "native"
    assert native["formal"]["result"]["completed"] is True


def test_missing_seed_folder_changes_nothing(tmp_path):
    target = tmp_path / "profiles"
    assert seed_default_profiles(tmp_path / "missing", target) == []
    assert not target.exists()
