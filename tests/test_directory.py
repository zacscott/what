"""Lists the files of a directory that is not a git repository."""

from pathlib import Path

import pytest

from what import directory
from what.config import settings


def test_files_skip_ignored_paths_and_dot_directories(tmp_path: Path) -> None:
    for path in ["app.py", "debug.log", "keep.log", "build/out.js", ".venv/lib.py", "src/a.py", "src/gen/b.py"]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x")
    (tmp_path / ".gitignore").write_text("*.log\n!keep.log\nbuild/\n")
    (tmp_path / "src/.gitignore").write_text("/gen/\n")

    assert sorted(directory.files(tmp_path)) == [".gitignore", "app.py", "keep.log", "src/.gitignore", "src/a.py"]


def test_files_stop_at_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_FILES", 2)
    for name in ["a", "b", "c"]:
        (tmp_path / name).write_text("x")
    with pytest.raises(directory.TooManyFiles, match="has more than 2 files"):
        directory.files(tmp_path)
