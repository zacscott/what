"""Keeps the index matching the working tree without reading git's list of tracked files on every search."""

from pathlib import Path

import pytest
from conftest import BENDER, git

from what import git as git_module
from what import search
from what.index import Index


def refuse_tracked_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make reading git's list of tracked files an error, so a test fails when a refresh reads it."""

    def refuse(root: Path) -> dict[str, str]:
        raise AssertionError("the refresh read git's list of tracked files")

    monkeypatch.setattr(git_module, "tracked_files", refuse)


def test_a_search_that_follows_no_git_change_does_not_list_the_tracked_files(
    index: Index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_tracked_files(monkeypatch)

    assert search.find(index, "bandana").text.splitlines() == [
        "src/BananaBender.php:3:public function bandana(int $count): string"
    ]


def test_edits_are_seen_without_listing_the_tracked_files(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refuse_tracked_files(monkeypatch)
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headband"))
    (repo / "src/peel.py").write_text("def peel_banana():\n    pass\n")

    assert "src/BananaBender.php:3:" in search.find(index, "headband").text
    assert "src/peel.py:1:def peel_banana():" in search.find(index, "peel_banana").text

    (repo / "src/peel.py").write_text("def peel_plantain():\n    pass\n")

    assert "src/peel.py:1:def peel_plantain():" in search.find(index, "peel_plantain").text


def test_an_untracked_file_that_is_removed_is_forgotten(index: Index, repo: Path) -> None:
    (repo / "src/peel.py").write_text("def peel_banana():\n    pass\n")
    assert "src/peel.py" in search.find(index, "peel_banana").text

    (repo / "src/peel.py").unlink()

    assert "src/peel.py" not in search.find(index, "peel_banana").text


def test_a_tracked_file_that_is_removed_from_disk_is_forgotten(index: Index, repo: Path) -> None:
    assert "src/BananaBender.php:3:" in search.find(index, "bandana").text

    (repo / "src/BananaBender.php").unlink()

    assert "src/BananaBender.php:3:" not in search.find(index, "bandana").text


def test_an_edit_that_git_changes_back_is_forgotten(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headband"))
    assert "src/BananaBender.php:3:" in search.find(index, "headband").text

    git(repo, "checkout", "--", "src/BananaBender.php")

    assert "src/BananaBender.php:3:" in search.find(index, "bandana").text
    assert "src/BananaBender.php" not in search.find(index, "headband").text


def test_a_commit_is_seen_by_the_next_search(index: Index, repo: Path) -> None:
    (repo / "src/peel.py").write_text("def peel_banana():\n    pass\n")
    assert "src/peel.py" in search.find(index, "peel_banana").text

    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Peel")
    (repo / "src/peel.py").write_text("def peel_plantain():\n    pass\n")

    assert "src/peel.py:1:def peel_plantain():" in search.find(index, "peel_plantain").text
    assert "src/peel.py" not in search.find(index, "peel_banana").text


def test_a_file_that_is_edited_then_staged_does_not_stop_the_shortcut(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headband"))
    (repo / "src/peel.py").write_text("def peel_banana():\n    pass\n")
    assert "src/BananaBender.php:3:" in search.find(index, "headband").text

    git(repo, "add", ".")
    assert "src/peel.py:1:" in search.find(index, "peel_banana").text
    refuse_tracked_files(monkeypatch)

    assert "src/BananaBender.php:3:" in search.find(index, "headband").text
