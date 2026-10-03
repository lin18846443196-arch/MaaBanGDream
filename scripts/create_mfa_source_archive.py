"""Export the exact desktop source and branding inputs used by a release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path


BRANDING_INPUTS = (
    "packaging/RhythmPilot.targets",
    "packaging/rhythmpilot.ico",
    "docs/assets/rhythmpilot-logo.png",
    "patches/rhythmpilot-mfa-branding.patch",
)
MFA_INPUTS = (
    "LICENSE",
    "MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj",
    "MFAUpdater/MFAUpdater.csproj",
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def clean_commit(root: Path) -> str:
    if git(root, "status", "--porcelain", "-z"):
        raise ValueError("source archives require clean Git working trees")
    # git archive 不展开子模块；宁可拒绝不完整源码，也不能静默漏掉依赖。
    if any(line.startswith(b"160000 ") for line in git(root, "ls-tree", "-r", "HEAD").splitlines()):
        raise ValueError("source archive needs explicit submodule source handling")
    return git(root, "rev-parse", "HEAD").decode().strip()


def export_source(package_root: Path, project_root: Path, mfa_root: Path) -> Path:
    info = json.loads((package_root / "BUILD-INFO.json").read_text(encoding="utf-8-sig"))
    version = info["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?", version):
        raise ValueError("invalid source archive version")
    if clean_commit(project_root) != info["maa_commit"] or clean_commit(mfa_root) != info["mfa_commit"]:
        raise ValueError("source commits differ from the package BUILD-INFO")
    name = f"RhythmPilot-v{version}-MFA-source"
    destination = package_root.parent / f"{name}.zip"
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with tempfile.TemporaryDirectory(dir=package_root.parent) as scratch:
            mfa_archive = Path(scratch) / "mfa.zip"
            brand_archive = Path(scratch) / "brand.zip"
            git(mfa_root, "archive", "--format=zip", f"--output={mfa_archive}", info["mfa_commit"])
            git(project_root, "archive", "--format=zip", f"--output={brand_archive}", info["maa_commit"], "--", *BRANDING_INPUTS)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
                for archive_path, folder, required in (
                    (mfa_archive, "MFAAvalonia", MFA_INPUTS),
                    (brand_archive, "build-inputs", BRANDING_INPUTS),
                ):
                    with zipfile.ZipFile(archive_path) as source:
                        if not set(required).issubset(source.namelist()):
                            raise ValueError("required source or branding input is missing")
                        for entry in source.infolist():
                            target = zipfile.ZipInfo(f"{name}/{folder}/{entry.filename}", entry.date_time)
                            target.external_attr = entry.external_attr
                            target.compress_type = zipfile.ZIP_DEFLATED
                            output.writestr(target, source.read(entry))
                output.writestr(f"{name}/SOURCE-INFO.json", json.dumps(info, ensure_ascii=False, indent=2) + "\n")
                output.writestr(f"{name}/README.md", """# RhythmPilot desktop corresponding source

MFAAvalonia/ contains the complete committed desktop and updater source, including
the RhythmPilot changes. Its GPL-3.0 license is in MFAAvalonia/LICENSE.
SOURCE-INFO.json identifies the exact commits used by the matching binary package.
build-inputs/ contains the icon, artwork, MSBuild targets and reconstruction patch.
Agent sources and release scripts are maintained independently at:
https://github.com/lin18846443196-arch/MaaBanGDream

To rebuild the desktop on Windows, install .NET SDK 10 and run from this directory:

```powershell
$brand = (Resolve-Path build-inputs/packaging).Path
$targets = Join-Path $brand 'RhythmPilot.targets'
dotnet publish MFAAvalonia/MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj -c Release -r win-x64 --self-contained true -p:MaaBanGDreamPackageBuild=true "-p:RhythmPilotBrandRoot=$brand" "-p:CustomAfterMicrosoftCommonTargets=$targets" -o output/desktop
dotnet publish MFAAvalonia/MFAUpdater/MFAUpdater.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:PublishTrimmed=true -p:TrimMode=link "-p:CustomAfterMicrosoftCommonTargets=$targets" -o output/updater
```

NuGet restores the public dependencies declared in the included project files.
Copy output/updater/MFAUpdater.exe into output/desktop for the portable host.
The branding patch is already applied; do not apply it again to this source tree.
""")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    Path(f"{destination}.sha256").write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    verify_source(package_root)
    return destination


def verify_source(package_root: Path) -> Path:
    info = json.loads((package_root / "BUILD-INFO.json").read_text(encoding="utf-8-sig"))
    name = f"RhythmPilot-v{info['version']}-MFA-source"
    path = package_root.parent / f"{name}.zip"
    expected = Path(f"{path}.sha256").read_text(encoding="utf-8").split()[0]
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest().lower() != expected.lower():
            raise ValueError("MFA source archive SHA256 mismatch")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("MFA source archive CRC check failed")
        metadata = json.loads(archive.read(f"{name}/SOURCE-INFO.json"))
        if metadata != info:
            raise ValueError("MFA source archive does not match the binary BUILD-INFO")
        for relative in MFA_INPUTS:
            archive.getinfo(f"{name}/MFAAvalonia/{relative}")
        for relative, field in (
            ("packaging/RhythmPilot.targets", "branding_targets_sha256"),
            ("patches/rhythmpilot-mfa-branding.patch", "mfa_branding_patch_sha256"),
        ):
            if hashlib.sha256(archive.read(f"{name}/build-inputs/{relative}")).hexdigest() != info[field]:
                raise ValueError("MFA source branding inputs differ from the binary build")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--mfa-root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        result = verify_source(args.package_root)
    else:
        if not args.project_root or not args.mfa_root:
            parser.error("export requires --project-root and --mfa-root")
        result = export_source(args.package_root, args.project_root, args.mfa_root)
    print(f"mfa_source_archive={result}")
    print("mfa_source_validation=passed")


if __name__ == "__main__":
    main()
