import os
from pathlib import Path
import sys

import pytest

from scripts.prepare_portable_runtime import prepare


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH regression")
def test_repair_accesses_long_paths_without_writing_extended_prefix(tmp_path):
    root = tmp_path / "中文 空格"
    (root / "Scripts").mkdir(parents=True)
    relative = Path("Lib") / ("long-directory-" * 5) / ("another-directory-" * 5) / "prefix.txt"
    target = root / relative
    extended = "\\\\?\\" + str(target.resolve())
    os.makedirs(os.path.dirname(extended))
    with open(extended, "w", encoding="utf-8") as writer:
        writer.write("BUILD_PREFIX")
    code = (
        "import os\n"
        "root = os.path.dirname(os.path.dirname(__file__))\n"
        f"target = os.path.join(root, {str(relative)!r}).replace('\\\\', '/')\n"
        "with open(target, 'r+', encoding='utf-8') as writer:\n"
        "    assert writer.read() == 'BUILD_PREFIX'\n"
        "    writer.seek(0); writer.write(root); writer.truncate()\n"
    )
    (root / "Scripts" / "conda-unpack-script.py").write_text(code, encoding="utf-8")
    arguments = sys.argv
    prepare(root)
    assert sys.argv is arguments
    with open(extended, encoding="utf-8") as reader:
        assert reader.read() == str(root.resolve())


def test_repair_rejects_escape_and_restores_arguments(tmp_path):
    (tmp_path / "Scripts").mkdir()
    script = tmp_path / "Scripts" / "conda-unpack-script.py"
    outside = tmp_path.parent / "outside.txt"
    script.write_text(f"open({str(outside)!r}, 'w')", encoding="utf-8")
    arguments = sys.argv
    with pytest.raises(ValueError, match="escapes"):
        prepare(tmp_path)
    assert sys.argv is arguments
    assert not outside.exists()
