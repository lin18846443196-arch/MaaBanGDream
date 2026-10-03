from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.create_mfa_source_archive import BRANDING_INPUTS, MFA_INPUTS, export_source, verify_source


def repository(root: Path, files: tuple[str, ...]) -> str:
    root.mkdir()
    for relative in (*files, ".gitignore"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bin/\nlogs/\n" if relative == ".gitignore" else relative, encoding="utf-8")
    for args in (
        ("init",),
        ("config", "user.name", "Source test"),
        ("config", "user.email", "source-test@example.invalid"),
        ("config", "core.autocrlf", "false"),
        ("add", "."),
        ("commit", "-m", "test source"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()


@pytest.fixture
def sources(tmp_path):
    project = tmp_path / "project"
    mfa = tmp_path / "mfa"
    info = {
        "version": "2.0.0",
        "maa_commit": repository(project, BRANDING_INPUTS),
        "mfa_commit": repository(mfa, MFA_INPUTS),
        "branding_targets_sha256": hashlib.sha256(BRANDING_INPUTS[0].encode()).hexdigest(),
        "mfa_branding_patch_sha256": hashlib.sha256(BRANDING_INPUTS[3].encode()).hexdigest(),
    }
    package = tmp_path / "release" / "YesBanGDream-v2.0.0-win-x64"
    package.mkdir(parents=True)
    (package / "BUILD-INFO.json").write_text(json.dumps(info), encoding="utf-8")
    return package, project, mfa, info


def test_corresponding_source_includes_committed_inputs_and_excludes_local_artifacts(sources):
    package, project, mfa, info = sources
    for root in (project, mfa):
        (root / "bin").mkdir()
        (root / "bin" / "private.txt").write_text("private", encoding="utf-8")
    path = export_source(package, project, mfa)
    assert verify_source(package) == path
    prefix = "YesBanGDream-v2.0.0-MFA-source/"
    with zipfile.ZipFile(path) as archive:
        assert not any("private" in name or "/.git/" in name for name in archive.namelist())
        assert archive.read(f"{prefix}MFAAvalonia/{MFA_INPUTS[1]}") == MFA_INPUTS[1].encode()
        assert json.loads(archive.read(f"{prefix}SOURCE-INFO.json")) == info
        assert b"dotnet publish" in archive.read(f"{prefix}README.md")


@pytest.mark.parametrize("root_index", [1, 2])
def test_dirty_source_cannot_be_delivered_as_matching_release(sources, root_index):
    package, project, mfa, _ = sources
    (sources[root_index] / "unexpected.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(ValueError, match="clean Git"):
        export_source(package, project, mfa)
    assert not list(package.parent.glob("*.zip"))


def test_wrong_build_commit_cannot_export_different_source(sources):
    package, project, mfa, info = sources
    info["mfa_commit"] = "0" * 40
    (package / "BUILD-INFO.json").write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(ValueError, match="source commits differ"):
        export_source(package, project, mfa)


def test_source_archive_corruption_blocks_release(sources):
    package, project, mfa, _ = sources
    path = export_source(package, project, mfa)
    with path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        verify_source(package)


def test_source_archive_from_other_binary_build_blocks_release(sources):
    package, project, mfa, info = sources
    export_source(package, project, mfa)
    info["mfa_commit"] = "f" * 40
    (package / "BUILD-INFO.json").write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        verify_source(package)
