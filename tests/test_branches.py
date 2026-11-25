"""Searches that reach the other branches of the repository, as well as the working tree."""

import subprocess
from pathlib import Path

from conftest import BENDER, git

from what import search
from what.index import Index


def commit_file(repo: Path, path: str, text: str, message: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)


def test_a_symbol_only_on_an_unmerged_branch_is_found_by_the_fallback(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/peel")
    commit_file(repo, "src/peel.py", "def peel_banana():\n    return 'peeled'\n", "Peel")
    git(repo, "checkout", "-q", "main")

    assert search.find(index, "peel_banana").splitlines() == [
        "not in this checkout; found on 1 other branch:",
        "feature/peel (just now) src/peel.py:1:def peel_banana():",
    ]
    assert search.grep(index, "peeled").splitlines() == [
        "not in this checkout; found on 1 other branch:",
        "1 hit in 1 file",
        "feature/peel (just now) src/peel.py-1-def peel_banana():",
        "feature/peel (just now) src/peel.py:2:return 'peeled'",
    ]


def test_a_symbol_in_the_working_tree_does_not_search_branches(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/rename")
    commit_file(repo, "src/BananaBender.php", BENDER.replace("$count", "$times"), "Rename")
    git(repo, "checkout", "-q", "main")

    assert "feature/rename" not in search.find(index, "bandana")
    assert "feature/rename" not in search.grep(index, "str_repeat")
    assert "feature/rename" in search.grep(index, "str_repeat", branches=True)


def test_a_branch_merged_into_the_default_branch_is_never_searched(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "merged")
    commit_file(repo, "src/BananaBender.php", BENDER + "function ripen() {}\n", "Ripen")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "merged")
    commit_file(repo, "src/BananaBender.php", BENDER, "Stop ripening")

    assert search.find(index, "ripen", kind="function").startswith("No function named 'ripen'")
    assert "merged" not in search.grep(index, "ripen", branches=True)


def test_a_file_that_is_the_same_on_a_branch_and_in_the_working_tree_is_listed_once(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "twin")
    commit_file(repo, "src/twin.py", "def twin_banana():\n    pass\n", "Twin")
    git(repo, "checkout", "-q", "main")
    commit_file(repo, "src/twin.py", "def twin_banana():\n    pass\n", "Same twin")

    assert search.find(index, "twin_banana", branches=True).splitlines() == ["src/twin.py:1:def twin_banana():"]
    assert search.grep(index, "twin_banana", branches=True).splitlines() == [
        "1 hit in 1 file",
        "src/twin.py:1:def twin_banana():" + search.DEFINITION_MARK,
    ]


def test_branches_option_lists_working_tree_and_branch_hits_together(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/rename")
    commit_file(repo, "src/BananaBender.php", BENDER.replace("$count", "$times"), "Rename")
    git(repo, "checkout", "-q", "main")

    assert search.grep(index, "str_repeat", branches=True).splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:return str_repeat('banana', $count);",
        "also on 1 other branch:",
        "1 hit in 1 file",
        "feature/rename (just now) src/BananaBender.php-3-public function bandana(int $times): string",
        "feature/rename (just now) src/BananaBender.php:4:return str_repeat('banana', $times);",
    ]


def test_the_default_branch_shows_what_the_checked_out_branch_changed(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/rename")
    commit_file(repo, "src/BananaBender.php", BENDER.replace("bandana", "headscarf"), "Rename the method")

    assert search.find(index, "bandana", kind="method").splitlines() == [
        "not in this checkout; found on 1 other branch:",
        "main (just now) src/BananaBender.php:3:public function bandana(int $count): string",
    ]


def test_deleted_files_and_branches_are_searched_together(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/sauce")
    commit_file(repo, "src/sauce.py", 'SAUCE = "cherry_sauce"\n', "Sauce")
    git(repo, "checkout", "-q", "main")

    output = search.grep(index, "cherry_sauce", deleted=True, branches=True)

    assert '^:legacy/split.py:2:topping = "cherry_sauce"' in output
    assert 'feature/sauce (just now) src/sauce.py:1:SAUCE = "cherry_sauce"' in output


def test_a_repository_with_no_remotes_has_no_fetch_note(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/peel")
    commit_file(repo, "src/peel.py", "def peel_banana():\n    pass\n", "Peel")
    git(repo, "checkout", "-q", "main")

    assert "remotes" not in search.find(index, "peel_banana")


def test_remote_branches_are_searched_and_say_when_they_were_fetched(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/peel")
    commit_file(repo, "src/peel.py", "def peel_banana():\n    pass\n", "Peel")
    git(repo, "checkout", "-q", "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True, capture_output=True)
    index = Index(clone, cache_dir=tmp_path / "clone-cache")

    assert search.find(index, "peel_banana").splitlines() == [
        "not in this checkout; found on 1 other branch:",
        "origin/feature/peel (just now) src/peel.py:1:def peel_banana():",
        "remotes not fetched since the clone",
    ]

    git(clone, "fetch", "-q")
    assert search.find(index, "peel_banana").splitlines()[-1] == "remotes fetched just now"
