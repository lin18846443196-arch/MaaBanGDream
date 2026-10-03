"""Run conda-unpack with correct Windows file access in deep Unicode paths."""
from __future__ import annotations

import argparse
import builtins
import os
from pathlib import Path
import runpy
import sys


def prepare(runtime_root: Path) -> None:
    root = runtime_root.resolve(strict=True)
    script = root / "Scripts" / "conda-unpack-script.py"
    if not script.is_file():
        raise FileNotFoundError(f"Portable runtime repair script is missing: {script}")

    def runtime_open(file, *args, **kwargs):
        # conda-pack 使用斜线访问文件，但 Windows 扩展路径只接受反斜线。
        # 仅改变文件 API 路径，写入环境的 prefix 仍为普通路径。
        path = os.path.abspath(os.path.normpath(os.fspath(file)))
        if os.path.commonpath((path, str(root))) != str(root):
            raise ValueError("Conda repair file escapes the portable runtime")
        if os.name == "nt" and not path.startswith("\\\\?\\"):
            path = "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path
        return builtins.open(path, *args, **kwargs)

    previous_argv = sys.argv
    previous_path = sys.path[:]
    try:
        sys.path.insert(0, str(script.parent))
        sys.argv = [str(script)]
        runpy.run_path(str(script), init_globals={"open": runtime_open}, run_name="__main__")
    finally:
        sys.argv = previous_argv
        sys.path[:] = previous_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_root", type=Path)
    prepare(parser.parse_args().runtime_root)
