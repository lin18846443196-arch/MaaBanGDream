"""发布包只补齐缺失的默认 Profile，不覆盖用户校准或选择。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def seed_default_profiles(source: Path, target: Path) -> list[str]:
    if not source.is_dir():
        return []
    imported: list[str] = []
    for path in sorted(source.glob("*.json")):
        data = path.read_bytes()
        json.loads(data.decode("utf-8-sig"))
        target.mkdir(parents=True, exist_ok=True)
        destination = target / path.name
        try:
            # 独占创建也保护并发启动；同名文件和已有选择始终归用户所有。
            with destination.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            continue
        imported.append(path.name)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    imported = seed_default_profiles(args.root / "default-profiles", args.root / "profiles")
    print(json.dumps({"default_profiles_imported": imported}, ensure_ascii=True))


if __name__ == "__main__":
    main()
