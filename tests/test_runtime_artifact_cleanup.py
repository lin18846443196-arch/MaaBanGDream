"""Exercise real deletion only in isolated disposable fake package trees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cleanup_runtime_artifacts as cleanup

NOW = 1_800_000_000.0
OLD = NOW - cleanup.RETENTION_SECONDS - 1
RECENT = NOW - 60


class PackageFixture(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="mbdr-cleanup-test-"))
        self.root = Path(temporary).resolve() / "package"
        self.root.mkdir()
        self.file("interface.template.json", "{}")
        self.file("agent/server.py", "# fake package")

    def file(self, relative, content="old diagnostic", modified=OLD):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.utime(path, (modified, modified))
        return path

    def age_directories(self, path):
        for child in path.rglob("*"):
            if child.is_dir():
                os.utime(child, (OLD, OLD))
        os.utime(path, (OLD, OLD))

    def clean(self, **kwargs):
        return cleanup.clean_runtime_artifacts(self.root, now=NOW, **kwargs)


class CleanupTests(PackageFixture):
    def test_removes_only_files_older_than_24_hours_in_owned_outputs(self):
        expired = [self.file(f"{tree}/old.png") for tree in cleanup.OUTPUT_TREES]
        kept = []
        for tree in cleanup.OUTPUT_TREES:
            for name, modified in (("boundary.json", NOW - cleanup.RETENTION_SECONDS),
                                   ("recent.log", RECENT), ("future.log", NOW + 60)):
                kept.append(self.file(f"{tree}/{name}", modified=modified))
        result = self.clean()
        self.assertEqual(result.removed_files, 3)
        self.assertEqual(result.freed_bytes, len("old diagnostic") * 3)
        self.assertTrue(all(not path.exists() for path in expired))
        self.assertTrue(all(path.exists() for path in kept))
        self.assertTrue(all((self.root / name).is_dir() for name in cleanup.OUTPUT_TREES))

    def test_configuration_profiles_resources_backups_and_temp_are_untouched(self):
        kept = [self.file(path) for path in (
            "config/instances/default.json", "profiles/expert.json", "resource/image/reference.png",
            "resource/charts/song.json", "backup/original.py", "runtime/python/python.exe",
            "tests/fixtures/example.png", "temp/test_script.py", "debug/config/maa_option.json",
        )]
        self.file("debug/obsolete.log")
        result = self.clean()
        self.assertEqual(result.removed_files, 1)
        self.assertTrue(all(path.exists() for path in kept))

    def test_manual_flow_recordings_are_kept_even_when_old(self):
        manual = self.file("debug/recordings/manual-flow-general-20260901/screen.mkv")
        self.age_directories(manual.parent)
        automatic = self.file("debug/recordings/cooperative-old/screen.mkv")
        self.age_directories(automatic.parent)
        self.clean()
        self.assertTrue(manual.exists())
        self.assertFalse(automatic.parent.exists())

    def test_recent_nested_file_keeps_whole_recording_bundle(self):
        old_video = self.file("debug/recordings/run/screen.mkv")
        recent_trace = self.file("debug/recordings/run/nested/trace.jsonl", modified=RECENT)
        self.age_directories(old_video.parent)
        result = self.clean()
        self.assertEqual(result.removed_files, 0)
        self.assertTrue(old_video.exists())
        self.assertTrue(recent_trace.exists())

    def test_recent_directory_or_empty_subdirectory_keeps_whole_bundle(self):
        video = self.file("debug/cooperative-startup/run/frame.png")
        self.age_directories(video.parent)
        nested = video.parent / "just-created"
        nested.mkdir()
        os.utime(nested, (RECENT, RECENT))
        self.clean()
        self.assertTrue(video.exists())

    def test_old_evidence_folders_removed_without_removing_output_roots(self):
        for tree in ("debug/cooperative-startup", "debug/team-live", "debug/recordings"):
            self.file(f"{tree}/old/nested/trace.jsonl")
            self.age_directories(self.root / tree)
        self.age_directories(self.root / "debug")
        result = self.clean()
        self.assertEqual(result.removed_files, 3)
        self.assertGreaterEqual(result.removed_directories, 6)
        self.assertTrue((self.root / "debug").is_dir())

    def test_unfinished_calibration_references_preserved_without_protecting_all_outputs(self):
        report = self.file("screencap/calibration.json", "{}")
        screenshot = self.file("screencap/calibration.png")
        video = self.file("debug/recordings/calibration-run/screen.mkv")
        self.age_directories(video.parent)
        session = {"status": "paused", "attempts": [
            {"report_path": "screencap/calibration.json",
             "recording_path": str(video.parent)},
        ]}
        self.file("profiles/calibration-sessions/current.json", json.dumps(session))
        obsolete = self.file("debug/obsolete.log")
        result = self.clean()
        self.assertEqual(result.removed_files, 1)
        self.assertFalse(obsolete.exists())
        self.assertTrue(all(path.exists() for path in (report, screenshot, video)))

    def test_completed_calibration_diagnostics_expire_but_profile_does_not(self):
        report = self.file("screencap/calibration.json", "{}")
        session_path = self.file("profiles/calibration-sessions/done.json", json.dumps({
            "status": "accepted", "attempts": [{"report_path": str(report)}],
        }))
        self.clean()
        self.assertFalse(report.exists())
        self.assertTrue(session_path.exists())

    def test_corrupt_calibration_metadata_keeps_evidence_and_cleans_logs(self):
        self.file("profiles/calibration-sessions/broken.json", "{")
        screenshot = self.file("screencap/old.png")
        recording = self.file("debug/old.log")
        log = self.file("logs/old.log")
        result = self.clean()
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.removed_files, 1)
        self.assertFalse(log.exists())
        self.assertTrue(screenshot.exists())
        self.assertTrue(recording.exists())

    def test_dry_run_does_not_change_files_or_directories(self):
        path = self.file("debug/recordings/expired/screen.mkv")
        self.age_directories(path.parent)
        result = self.clean(dry_run=True)
        self.assertEqual(result.expired_files, 1)
        self.assertEqual(result.removed_files, 0)
        self.assertEqual(result.freed_bytes, 0)
        self.assertTrue(path.exists())

    def test_locked_file_is_skipped_and_other_expired_files_are_removed(self):
        locked = self.file("logs/locked.log")
        ordinary = self.file("logs/old.log")
        real_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == locked:
                raise PermissionError("file is in use")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            result = self.clean()
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.removed_files, 1)
        self.assertTrue(locked.exists())
        self.assertFalse(ordinary.exists())

    def test_modification_after_scan_prevents_file_deletion(self):
        path = self.file("logs/old.log")
        real_remove = cleanup._remove_file
        def changed(path, *args):
            os.utime(path, (RECENT, RECENT))
            real_remove(path, *args)
        with patch.object(cleanup, "_remove_file", changed):
            self.clean()
        self.assertTrue(path.exists())

    def test_idempotent_and_absent_trees_are_not_created(self):
        self.assertEqual(self.clean().errors, 0)
        self.assertFalse((self.root / "debug").exists())
        self.file("logs/old.log")
        self.assertEqual(self.clean().removed_files, 1)
        self.assertEqual(self.clean().removed_files, 0)

    def test_non_package_root_refused(self):
        with self.assertRaises(ValueError):
            cleanup.clean_runtime_artifacts(self.root.parent, now=NOW)

    def test_windows_junction_never_traversed_or_removed(self):
        if os.name != "nt":
            self.skipTest("Windows junction regression")
        outside = self.root.parent / "external-artifacts"
        outside.mkdir()
        sentinel = outside / "old.png"
        sentinel.write_text("do not delete", encoding="utf-8")
        os.utime(sentinel, (OLD, OLD))
        (self.root / "debug").mkdir()
        link = self.root / "debug" / "linked"
        # Create a junction using cmd only; all cleanup uses pathlib end-to-end.
        process = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                                 capture_output=True, check=True)
        self.assertEqual(process.returncode, 0)
        try:
            result = self.clean()
            self.assertEqual(result.skipped_links, 1)
            self.assertTrue(link.is_dir())
            self.assertTrue(sentinel.exists())
        finally:
            # Explicitly remove the junction itself, never recursively its target.
            os.rmdir(link)

    def test_running_application_skips_cleanup(self):
        old = self.file("logs/old.log")
        with patch.object(cleanup, "mfa_is_running", return_value=True), \
                patch.object(sys, "argv", ["cleanup", "--root", str(self.root), "--skip-if-running"]), \
                patch.object(cleanup, "clean_runtime_artifacts") as clean:
            self.assertEqual(cleanup.main(), 0)
        clean.assert_not_called()
        self.assertTrue(old.exists())

    def test_process_guard_only_matches_this_installation(self):
        import psutil
        current = Mock(info={"name": "MFAAvalonia.exe", "exe": str(self.root / "MFAAvalonia.exe")})
        other = Mock(info={"name": "MFAAvalonia.exe", "exe": str(self.root.parent / "other/MFAAvalonia.exe")})
        with patch.object(psutil, "process_iter", return_value=[other]):
            self.assertFalse(cleanup.mfa_is_running(self.root))
        with patch.object(psutil, "process_iter", return_value=[current]):
            self.assertTrue(cleanup.mfa_is_running(self.root))

    def test_branded_hosts_are_protected_during_migration(self):
        import psutil
        for name in ("YesBanGDream.exe", "MaaBanGDream.exe"):
            with self.subTest(name=name):
                process = Mock(info={"name": name, "exe": str(self.root / name)})
                with patch.object(psutil, "process_iter", return_value=[process]):
                    self.assertTrue(cleanup.mfa_is_running(self.root))


class LauncherTests(PackageFixture):
    """Run the real launcher in a fake package; intercept GUI launch completely."""
    def setUp(self):
        super().setUp()
        if os.name != "nt":
            self.skipTest("Windows release launcher")
        self.powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.file("YesBanGDream.exe", "dummy; never executed")
        self.file("runtime/python/.maabangdream-ready", "ready")
        self.file("scripts/check_runtime.py", "# isolated compatibility stub")
        self.file("scripts/sync_bestdori_catalog.py", "# unused by launcher")
        self.file("agent/profile_manager.py", "# unused by launcher")
        self.file("resource/charts/manifest.json", "{}")
        self.file("interface.template.json", json.dumps({
            "resource": [{"path": []}], "agent": {"child_exec": "", "child_args": []},
        }))
        self.file("scripts/cleanup_runtime_artifacts.py", (ROOT / "scripts/cleanup_runtime_artifacts.py").read_text(encoding="utf-8"))
        source = (ROOT / "scripts/start-release.ps1").read_text(encoding="utf-8-sig")
        # Use the actual bundled Python without copying hundreds of MiB of runtime.
        source = source.replace("$python = Join-Path $pythonRoot 'python.exe'",
                                "$python = '" + sys.executable.replace("'", "''") + "'")
        (self.root / "scripts/start-release.ps1").write_text(source, encoding="utf-8-sig")
        self.log = self.file("logs/obsolete.log", modified=time.time() - 90_000)

    def launch(self, no_launch=False):
        script = str(self.root / "scripts/start-release.ps1").replace("'", "''")
        marker = str(self.root / "gui-launch-intercepted.txt").replace("'", "''")
        harness = self.root / "launcher-test.ps1"
        harness.write_text(
            "function Start-Process { param($FilePath, $WorkingDirectory) "
            f"[System.IO.File]::WriteAllText('{marker}', 'intercepted') }}\n"
            f"& '{script}'" + (" -NoLaunch" if no_launch else ""), encoding="utf-8-sig")
        return subprocess.run([str(self.powershell), "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(harness)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)

    def test_launcher_cleans_before_gui_launch(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.log.exists())
        self.assertTrue((self.root / "gui-launch-intercepted.txt").exists())
        self.assertIn("Runtime artifact cleanup: removed 1 files", result.stdout)

    def test_launcher_nolaunch_keeps_existing_artifacts(self):
        result = self.launch(no_launch=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.exists())
        self.assertFalse((self.root / "gui-launch-intercepted.txt").exists())

    def test_launcher_still_launches_when_cleanup_fails(self):
        (self.root / "scripts/cleanup_runtime_artifacts.py").write_text(
            "raise SystemExit(1)", encoding="utf-8")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.exists())
        self.assertTrue((self.root / "gui-launch-intercepted.txt").exists())


if __name__ == "__main__":
    unittest.main()
