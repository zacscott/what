"""The small git repository every test searches."""

import subprocess
from pathlib import Path

import pytest

from what.index import Index

BENDER = """<?php
class BananaBender extends Lady {
    public function bandana(int $count): string {
        return str_repeat('banana', $count);
    }
}
"""

SPLIT = """def banana_split(scoops: int) -> str:
    topping = "cherry_sauce"
    return topping * scoops
"""


def git(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(root), *identity, *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository where the first commit adds two files and the second deletes legacy/split.py."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "legacy").mkdir()
    (root / "src/BananaBender.php").write_text(BENDER)
    (root / "legacy/split.py").write_text(SPLIT)

    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "Add fruit")
    git(root, "rm", "-q", "legacy/split.py")
    git(root, "commit", "-q", "-m", "Remove the split")

    return root


@pytest.fixture
def index(repo: Path, tmp_path: Path) -> Index:
    index = Index(repo, cache_dir=tmp_path / "cache")
    index.refresh()
    index.refresh_history()
    return index


@pytest.fixture
def plain_directory(tmp_path: Path) -> Path:
    """A directory that is not a git repository, with one file the index should skip."""
    root = tmp_path / "plain"
    (root / "src").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "src/BananaBender.php").write_text(BENDER)
    (root / "build/BananaBender.php").write_text(BENDER)
    (root / ".gitignore").write_text("build/\n")

    return root
