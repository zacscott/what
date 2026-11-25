"""Runs the four searches against a small git repository built for each test."""

import subprocess
from pathlib import Path

import pytest
from conftest import BENDER, SPLIT, git

from what import search
from what.config import settings
from what.git import UnknownRef
from what.index import Index


def test_find_lists_definitions_under_their_path(index: Index) -> None:
    assert search.find(index, "banana").splitlines() == [
        "src/BananaBender.php:2:class BananaBender extends Lady",
        deleted(index, "legacy/split.py") + ":1:def banana_split(scoops: int) -> str:",
    ]


def test_find_ranks_an_exact_name_first_and_filters_by_kind(index: Index) -> None:
    assert search.find(index, "bandana", kind="method").splitlines() == [
        "src/BananaBender.php:3:public function bandana(int $count): string",
    ]


def test_find_with_a_kind_that_matches_nothing_does_not_search_text(index: Index) -> None:
    assert search.find(index, "str_repeat", kind="class").startswith("No class named 'str_repeat'.")


def test_find_sees_an_uncommitted_file_and_forgets_it_when_removed(
    index: Index,
    repo: Path,
) -> None:
    (repo / "src/fresh.py").write_text("def fresh_banana():\n    pass\n")
    assert "src/fresh.py:1:def fresh_banana():" in search.find(index, "fresh_banana")

    (repo / "src/fresh.py").unlink()
    assert search.find(index, "fresh_banana").startswith("Nothing named or containing")


def test_find_sees_an_uncommitted_edit_to_a_tracked_file(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    assert "src/BananaBender.php:3:public function headscarf(int $count): string" in search.find(index, "headscarf")
    assert "src/BananaBender.php" not in search.find(index, "bandana")


def test_find_falls_back_to_text_when_no_symbol_matches(index: Index) -> None:
    assert search.find(index, "str_repeat").splitlines() == [
        "No symbol or path named 'str_repeat'. Text matches:",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:return str_repeat('banana', $count);",
    ]


def test_find_matches_an_abbreviation_only_at_the_start_of_words(index: Index, repo: Path) -> None:
    (repo / "src/controllers.php").write_text(
        "<?php\nclass RequestController {}\nclass FrequencyController {}\nclass ControllerRequest {}\n"
    )

    assert search.find(index, "reqController").splitlines() == [
        "src/controllers.php:2:class RequestController",
    ]
    assert search.find(index, "reqcontroller") == search.find(index, "reqController")
    assert search.find(index, "reqCont", kind="class").splitlines() == [
        "src/controllers.php:2:class RequestController",
    ]


def test_find_matches_a_path_by_abbreviation(index: Index) -> None:
    assert search.find(index, "legSplit") == deleted(index, "legacy/split.py")


def test_find_skips_abbreviations_when_a_closer_match_exists(index: Index, repo: Path) -> None:
    assert "split.py:1:def banana_split(" in search.find(index, "banSplit")
    (repo / "src/b.py").write_text("def banSplit():\n    pass\n")
    assert search.find(index, "banSplit").splitlines() == ["src/b.py:1:def banSplit():"]


def test_find_matches_a_file_by_path(index: Index) -> None:
    assert search.find(index, "split.py") == deleted(index, "legacy/split.py")


def test_grep_names_the_enclosing_symbol(index: Index) -> None:
    assert search.grep(index, "str_repeat").splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:return str_repeat('banana', $count);",
    ]


def test_grep_searches_deleted_files_when_no_live_file_matches(index: Index) -> None:
    split = deleted(index, "legacy/split.py")
    assert search.grep(index, "cherry_sauce").splitlines() == [
        "1 hit in 1 file",
        split + "-1-def banana_split(scoops: int) -> str:",
        split + ':2:topping = "cherry_sauce"',
    ]


def test_grep_marks_the_line_that_defines_a_matching_symbol(index: Index, repo: Path) -> None:
    (repo / "src/caller.php").write_text("<?php\nfunction feed() {\n    return (new BananaBender())->bandana(2);\n}\n")

    output = search.grep(index, "bandana", path="src/").splitlines()

    assert output[0] == "2 hits in 2 files"
    assert sorted(output[1:]) == [
        "src/BananaBender.php:3:public function bandana(int $count): string {" + search.DEFINITION_MARK,
        "src/caller.php-2-function feed()",
        "src/caller.php:3:return (new BananaBender())->bandana(2);",
    ]


def test_grep_marks_a_definition_when_the_pattern_holds_more_than_the_name(index: Index) -> None:
    assert search.grep(index, "function bandana").splitlines()[1].endswith(search.DEFINITION_MARK)
    assert not search.grep(index, "int $count").splitlines()[1].endswith(search.DEFINITION_MARK)


def test_grep_counts_the_hits_it_leaves_out(index: Index, repo: Path) -> None:
    for number in range(5):
        (repo / f"src/many{number}.txt").write_text("pip\npip\n")

    output = search.grep(index, "pip", path="many", limit=3).splitlines()

    assert output[0] == "10 hits in 5 files, 4 shown. Use --limit or --path to see more."
    assert len([line for line in output if ":pip" in line]) == 4


def test_grep_reads_past_the_count_limit_until_it_has_shown_its_limit(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "COUNT_FILES", 1)
    (repo / "src/q.txt").write_text("qxq\n")

    assert "src/q.txt:1:qxq" in search.grep(index, "q.q", regex=True)


def test_grep_prints_the_history_notice_only_for_a_deleted_file_search(index: Index) -> None:
    with index.connect() as session:
        session.delete_meta("history_head")

    assert search.HISTORY_BUILDING not in search.grep(index, "str_repeat")
    assert search.HISTORY_BUILDING not in search.find(index, "banana")
    assert search.HISTORY_BUILDING in search.grep(index, "str_repeat", deleted=True)


def test_grep_with_a_regex_and_a_pattern_too_short_for_the_index(index: Index) -> None:
    assert ":4:return str_repeat" in search.grep(index, r"str_\w+\('banana'", regex=True)
    assert ":2:class BananaBender extends Lady {" in search.grep(index, "{")


def test_grep_is_case_sensitive_only_when_the_pattern_has_a_capital(index: Index) -> None:
    assert "class BananaBender" in search.grep(index, "bananabender")
    assert search.grep(index, "BANANAbender").startswith("No text matches")


def test_grep_lists_commits_that_removed_the_text(index: Index) -> None:
    assert "Remove the split" in search.grep(index, "cherry_sauce", commits=True)


def test_outline_indents_nested_symbols(index: Index) -> None:
    assert search.outline(index, "BananaBender.php").splitlines() == [
        "src/BananaBender.php",
        "  2-6 class BananaBender extends Lady",
        "    3-5 public function bandana(int $count): string",
    ]


def test_show_prints_one_symbol(index: Index) -> None:
    assert search.show(index, "BananaBender.bandana").splitlines() == [
        "src/BananaBender.php:3-5",
        "    public function bandana(int $count): string {",
        "        return str_repeat('banana', $count);",
        "    }",
    ]


def test_show_prints_a_deleted_file_and_a_line_range(index: Index) -> None:
    split = deleted(index, "legacy/split.py")
    assert search.show(index, "legacy/split.py") == split + ":1-3\n" + SPLIT.rstrip("\n")
    assert search.show(index, split + ":2") == split + ":2-2\n" + '    topping = "cherry_sauce"'
    assert search.show(index, "src/BananaBender.php:2-2").splitlines()[1] == "class BananaBender extends Lady {"


def test_lookup_finds_a_name_that_is_not_a_file(index: Index) -> None:
    assert search.lookup(index, "banana") == search.find(index, "banana")
    assert search.lookup(index, "BananaBender.bandana") == search.find(index, "BananaBender.bandana")


def test_lookup_treats_a_plain_word_as_a_name_even_when_a_file_has_that_name(
    index: Index,
    repo: Path,
) -> None:
    (repo / "bin").mkdir()
    (repo / "bin/bandana").write_text("#!/bin/sh\necho bandana\n")

    assert search.lookup(index, "bandana") == search.find(index, "bandana")
    assert search.lookup(index, "bin/bandana") == "bin/bandana:1-2\n#!/bin/sh\necho bandana"


def test_lookup_outlines_a_file_live_or_deleted(index: Index) -> None:
    assert search.lookup(index, "BananaBender.php") == search.outline(index, "src/BananaBender.php")
    assert search.lookup(index, "split.py") == search.outline(index, "legacy/split.py")
    assert search.lookup(index, deleted(index, "legacy/split.py")) == search.outline(index, "legacy/split.py")


def test_lookup_shows_a_file_that_defines_nothing(index: Index, repo: Path) -> None:
    (repo / "notes.txt").write_text("bananas are berries\n")
    assert search.lookup(index, "notes.txt") == "notes.txt:1-1\nbananas are berries"


def test_lookup_shows_a_line_range(index: Index) -> None:
    assert search.lookup(index, "src/BananaBender.php:2-3") == search.show(index, "src/BananaBender.php:2-3")


def test_lookup_with_a_kind_searches_names_only(index: Index) -> None:
    assert search.lookup(index, "split.py", kind="function").startswith("No function named 'split.py'.")


def test_a_file_added_back_is_no_longer_listed_as_deleted(index: Index, repo: Path) -> None:
    (repo / "legacy").mkdir()
    (repo / "legacy/split.py").write_text(SPLIT)
    assert search.find(index, "banana_split").splitlines() == [
        "legacy/split.py:1:def banana_split(scoops: int) -> str:",
    ]


def test_history_follows_head_to_another_branch(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "before", "HEAD~1")
    index.refresh_history()
    assert "^:legacy/split.py" not in search.find(index, "banana_split")

    git(repo, "checkout", "-q", "main")
    index.refresh_history()
    assert "^:legacy/split.py" in search.find(index, "banana_split")


@pytest.mark.parametrize(
    ("pattern", "literals"),
    [
        (r"class \w+Controller", ["class ", "Controller"]),
        (r"colou?r_name", ["colo", "r_name"]),
        (r"foo|bar", []),
        (r"(foo)?bar", []),
        (r"get[A-Z]\w+Name", ["get", "Name"]),
        (r"a{2,3}banana", ["banana"]),
        (r"value\.length", ["value.length"]),
        (r"[]abc]def", []),
        (r"ab.cd", []),
        (r"\x41bcd", []),
        (r"foo\101bar", []),
        (r"[\]abc]zzz", []),
    ],
)
def test_required_literals(pattern: str, literals: list[str]) -> None:
    assert search.required_literals(pattern) == literals


def deleted(index: Index, path: str) -> str:
    """How output names a file that the last commit on main deleted, such as "a1b2c3d4^:legacy/split.py"."""
    log = subprocess.run(
        ["git", "-C", str(index.root), "log", "-1", "--format=%H", "main"], check=True, capture_output=True
    )
    return f"{log.stdout.decode().strip()[:8]}^:{path}"


def test_show_finds_one_method_among_hundreds_with_the_same_name(index: Index, repo: Path) -> None:
    classes = [f"class C{number}:\n    def run(self):\n        return {number}\n" for number in range(400)]
    (repo / "src/many.py").write_text("\n".join(classes))
    assert "return 399" in search.show(index, "C399.run")
    assert "def run(self):" in search.find(index, "run", path="many", kind="function", limit=500)


def test_the_same_content_is_indexed_again_under_another_extension(
    index: Index,
    repo: Path,
) -> None:
    (repo / "src/first.lock").write_text(SPLIT)
    search.find(index, "first.lock")
    (repo / "src/second.py").write_text(SPLIT)

    assert "src/second.py:1:def banana_split" in search.find(index, "banana_split")
    assert search.grep(index, "cherry_sauce").splitlines()[1].startswith("src/second.py")


def test_line_numbers_ignore_form_feeds(index: Index, repo: Path) -> None:
    (repo / "src/feed.py").write_text("x = 1\f\n\n\ndef needle():\n    return 'haystack'\n")
    assert search.grep(index, "haystack").splitlines() == [
        "1 hit in 1 file",
        "src/feed.py-4-def needle():",
        "src/feed.py:5:return 'haystack'",
    ]
    assert search.show(index, "needle").splitlines()[1] == "def needle():"


def test_an_untracked_symlink_is_not_followed(index: Index, repo: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("secret_outside_repo\n")
    (repo / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert search.grep(index, "secret_outside_repo").startswith("No text matches")


def test_a_search_while_the_history_build_is_unfinished_keeps_the_deleted_files(
    index: Index,
) -> None:
    with index.connect() as session:
        session.delete_meta("history_head")
    index.refresh()
    assert "^:legacy/split.py" in search.find(index, "banana_split")

    index.refresh_history()
    assert index.history_is_current()


def test_directory_mode_indexes_files_from_disk(plain_directory: Path, tmp_path: Path) -> None:
    index = Index(plain_directory, cache_dir=tmp_path / "cache")
    assert not index.in_git

    assert search.find(index, "bandana").splitlines() == [
        "src/BananaBender.php:3:public function bandana(int $count): string",
    ]
    assert search.grep(index, "str_repeat", commits=True).splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:return str_repeat('banana', $count);",
        "Commit search needs a git repository.",
    ]


def test_directory_mode_sees_edits_and_removals(plain_directory: Path, tmp_path: Path) -> None:
    index = Index(plain_directory, cache_dir=tmp_path / "cache")
    search.find(index, "bandana")
    (plain_directory / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    (plain_directory / "src/fresh.py").write_text("def fresh_banana():\n    pass\n")

    assert "src/BananaBender.php:3:public function headscarf(int $count): string" in search.find(index, "headscarf")
    assert "src/fresh.py" in search.find(index, "fresh_banana")

    (plain_directory / "src/fresh.py").unlink()
    assert search.find(index, "fresh_banana").startswith("Nothing named or containing")


def test_part_of_a_name_is_found_after_edits_and_the_name_index_stays_whole(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    assert ":3:public function headscarf(" in search.find(index, "adscar")

    (repo / "src/BananaBender.php").write_text(BENDER)
    assert "headscarf" not in search.find(index, "adscar")
    assert ":3:public function bandana(" in search.find(index, "ndan")

    with index.connect() as session:
        session.insert_symbol_name("integrity-check")


def test_show_at_a_ref_prints_the_body_a_method_had_then(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("'banana', $count", "'plantain', $count * 2"))
    git(repo, "commit", "-q", "-am", "Bend plantains")

    assert "plantain" in search.show(index, "BananaBender.bandana")
    assert search.show(index, "BananaBender.bandana", ref="HEAD~1").splitlines() == [
        "HEAD~1:src/BananaBender.php:3-5",
        "    public function bandana(int $count): string {",
        "        return str_repeat('banana', $count);",
        "    }",
    ]


def test_find_at_a_ref_finds_a_definition_that_was_deleted_later(index: Index) -> None:
    assert search.find(index, "banana_split", ref="HEAD~1").splitlines() == [
        "HEAD~1:legacy/split.py:1:def banana_split(scoops: int) -> str:",
    ]
    assert search.find(index, "banana_split", ref="main").startswith(
        "Nothing named or containing 'banana_split' at main"
    )


def test_find_at_a_ref_finds_a_class_that_was_deleted_later(index: Index, repo: Path) -> None:
    git(repo, "rm", "-q", "src/BananaBender.php")
    git(repo, "commit", "-q", "-m", "Remove the bender")

    assert search.find(index, "BananaBender", kind="class", ref="HEAD~1").splitlines() == [
        "HEAD~1:src/BananaBender.php:2:class BananaBender extends Lady",
    ]


def test_outline_at_a_ref_reads_the_file_from_git(index: Index) -> None:
    assert search.outline(index, "split.py", ref="HEAD~1").splitlines() == [
        "HEAD~1:legacy/split.py",
        "  1-3 def banana_split(scoops: int) -> str:",
    ]


def test_show_at_a_ref_says_when_the_symbol_is_not_there(index: Index) -> None:
    assert search.show(index, "banana_split", ref="main") == "No symbol or file called 'banana_split' at main."


def test_an_unknown_ref_raises_an_error_that_names_it(index: Index) -> None:
    with pytest.raises(UnknownRef, match="'no-such-branch' is not a branch, tag or commit"):
        search.find(index, "banana", ref="no-such-branch")
