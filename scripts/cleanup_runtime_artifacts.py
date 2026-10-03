"""Remove expired automatic diagnostics from the three owned output trees."""
from __future__ import annotations

import argparse
import json
import os
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


RETENTION_SECONDS = 24 * 60 * 60
OUTPUT_TREES = ("debug", "logs", "screencap")
# A recording/startup evidence folder is one record. Keep the entire record
# when any of its files or directories were modified within the last day.
SESSION_TREES = {("debug", "recordings"), ("debug", "cooperative-startup"), ("debug", "team-live")}


@dataclass
class CleanupResult:
    dry_run: bool
    expired_files: int = 0
    removed_files: int = 0
    removed_directories: int = 0
    expired_bytes: int = 0
    freed_bytes: int = 0
    skipped_links: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, path: Path, exc: object) -> None:
        self.errors += 1
        # Avoid flooding the launch console when a directory is inaccessible.
        if len(self.warnings) < 5:
            self.warnings.append(f"{path}: {exc}")


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _scan_info(path: Path, result: CleanupResult) -> os.stat_result | None:
    """Check each entry as traversal reaches it, without following links."""
    try:
        info = path.lstat()
        if _is_link(info):
            result.skipped_links += 1
            return None
        return info
    except FileNotFoundError:
        return None
    except OSError as exc:
        result.warn(path, exc)
        return None


def _safe_info(path: Path, root: Path, result: CleanupResult) -> os.stat_result | None:
    """Recheck every ancestor; never traverse a Windows junction or symlink."""
    try:
        relative = path.relative_to(root)
        current = root
        info = current.lstat()
        if _is_link(info):
            result.skipped_links += 1
            return None
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if _is_link(info):
                result.skipped_links += 1
                return None
        # Validate the resolved absolute destination immediately before mutation.
        if not path.resolve().is_relative_to(root):
            raise ValueError("artifact path escapes the package root")
        return info
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        result.warn(path, exc)
        return None


def _children(path: Path, result: CleanupResult) -> list[Path] | None:
    try:
        with os.scandir(path) as entries:
            return [path / entry.name for entry in entries]
    except OSError as exc:
        result.warn(path, exc)
        return None


def _protected_calibration_paths(root: Path, result: CleanupResult) -> set[Path]:
    """Preserve evidence still needed to resume an unfinished calibration."""
    protected: set[Path] = set()
    sessions = root / "profiles" / "calibration-sessions"
    if not sessions.is_dir():
        return protected
    for session in sessions.glob("*.json"):
        try:
            value = json.loads(session.read_text(encoding="utf-8-sig"))
            if value.get("status") not in {"active", "paused"}:
                continue
            for attempt in value.get("attempts", []):
                for name in ("report_path", "recording_path"):
                    raw = attempt.get(name)
                    if not isinstance(raw, str) or not raw:
                        continue
                    path = Path(raw)
                    if not path.is_absolute():
                        path = root / path
                    path = path.resolve()
                    if path != root and path.is_relative_to(root) and path.relative_to(root).parts[0] in OUTPUT_TREES:
                        protected.add(path)
                        # Results may have a same-name PNG beside their JSON.
                        if path.suffix == ".json":
                            protected.add(path.with_suffix(".png"))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            # Corrupt session metadata must not cause loss of resumable evidence.
            result.warn(session, exc)
            protected.update({root / "debug", root / "screencap"})
    return protected


def _protected(path: Path, root: Path, preserved: set[Path]) -> bool:
    relative = path.relative_to(root).parts
    if relative[:2] == ("debug", "config"):
        return True
    if len(relative) >= 3 and relative[:2] == ("debug", "recordings"):
        if relative[2].startswith("manual-flow-"):
            return True
    return any(path == kept or path.is_relative_to(kept)
               for kept in preserved)


def _remove_file(path: Path, root: Path, cutoff: float, result: CleanupResult) -> None:
    # Read-only scans check individual entries; only real deletion needs a
    # fresh check of every ancestor and the resolved destination.
    info = _scan_info(path, result) if result.dry_run else _safe_info(path, root, result)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
        return
    result.expired_files += 1
    result.expired_bytes += info.st_size
    if result.dry_run:
        return
    try:
        path.unlink()
        result.removed_files += 1
        result.freed_bytes += info.st_size
    except OSError as exc:
        # Locked/read-only logs are skipped; do not interrupt application startup.
        result.warn(path, exc)


def _remove_empty(path: Path, root: Path, result: CleanupResult) -> None:
    if result.dry_run or _safe_info(path, root, result) is None:
        return
    try:
        # No recursive delete: a newly created file automatically prevents rmdir.
        if next(path.iterdir(), None) is None:
            path.rmdir()
            result.removed_directories += 1
    except OSError as exc:
        result.warn(path, exc)


def _session_expired(path: Path, root: Path, cutoff: float,
                     result: CleanupResult, preserved: set[Path]) -> bool:
    info = _scan_info(path, result)
    if info is None or _protected(path, root, preserved) or info.st_mtime >= cutoff:
        return False
    if stat.S_ISREG(info.st_mode):
        return True
    if not stat.S_ISDIR(info.st_mode):
        return False
    children = _children(path, result)
    return children is not None and all(
        _session_expired(child, root, cutoff, result, preserved) for child in children
    )


def clean_runtime_artifacts(package_root: Path, *, dry_run: bool = False,
                            now: float | None = None) -> CleanupResult:
    root = Path(package_root).resolve(strict=True)
    # Only an installed MaaBanGDream package is a valid cleanup root.
    if not (root / "interface.template.json").is_file() or not (root / "agent/server.py").is_file():
        raise ValueError(f"Not a MaaBanGDream package: {root}")
    result = CleanupResult(dry_run=dry_run)
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    preserved = _protected_calibration_paths(root, result)

    def visit(path: Path, *, keep_directory: bool = False) -> None:
        if _protected(path, root, preserved):
            return
        info = _scan_info(path, result)
        if info is None:
            return
        if stat.S_ISREG(info.st_mode):
            _remove_file(path, root, cutoff, result)
            return
        if not stat.S_ISDIR(info.st_mode):
            return
        children = _children(path, result)
        if children is None:
            return
        relative = path.relative_to(root).parts
        for child in children:
            if relative in SESSION_TREES:
                # Do not partially dismantle a recent evidence bundle.
                if not _session_expired(child, root, cutoff, result, preserved):
                    continue
            visit(child)
        if not keep_directory and info.st_mtime < cutoff:
            _remove_empty(path, root, result)

    for name in OUTPUT_TREES:
        visit(root / name, keep_directory=True)
    return result


def mfa_is_running(root: Path) -> bool:
    # psutil is supplied with the verified portable runtime. If a process has
    # denied access to its executable, retain its records instead of guessing.
    import psutil

    host_names = {"mfaavalonia.exe", "maabangdream.exe", "rhythmpilot.exe"}
    for process in psutil.process_iter(["name", "exe"]):
        try:
            name = (process.info["name"] or "").casefold()
            if name not in host_names:
                continue
            executable = process.info["exe"]
            if executable is None or Path(executable).resolve().parent == root.resolve():
                return True
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true", help="List totals without deleting any files")
    parser.add_argument("--skip-if-running", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable cleanup totals")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.skip_if_running and mfa_is_running(root):
            print("Runtime artifact cleanup skipped: RhythmPilot is already running.")
            return 0
        result = clean_runtime_artifacts(root, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(asdict(result), ensure_ascii=False))
        else:
            count = result.expired_files if args.dry_run else result.removed_files
            size = result.expired_bytes if args.dry_run else result.freed_bytes
            verb = "would remove" if args.dry_run else "removed"
            print(f"Runtime artifact cleanup: {verb} {count} files older than 24 hours "
                  f"({size / 1024**2:.1f} MiB); {result.errors} errors skipped.")
            for warning in result.warnings:
                print(f"Cleanup warning: {warning}")
        # Per-file failures are already reported and are nonfatal for startup.
        return 0
    except Exception as exc:
        print(f"Runtime artifact cleanup skipped: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
