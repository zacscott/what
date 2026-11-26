"""Runs the four searches against a small git repository built for each test."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import BENDER, SPLIT, git

from what import git as git_module
from what import search
from what.config import settings
from what.database import IndexSession
from what.git import UnknownRef
from what.index import Index, Reading
from what.output import Span
from what.search import Case, at_ref
from what.symbols import Kind


def test_find_lists_definitions_under_their_path(index: Index) -> None:
    assert search.find(index, "banana").text.splitlines() == [
        "src/BananaBender.php:2:class BananaBender extends Lady",
        deleted(index, "legacy/split.py") + ":1:def banana_split(scoops: int) -> str:",
    ]


def test_find_ranks_an_exact_name_first_and_filters_by_kind(index: Index) -> None:
    assert search.find(index, "bandana", kind=Kind.METHOD).text.splitlines() == [
        "src/BananaBender.php:3:public function bandana(int $count): string",
    ]


def test_find_with_a_kind_that_matches_nothing_does_not_search_text(index: Index) -> None:
    assert search.find(index, "str_repeat", kind=Kind.CLASS).text.startswith("No class named 'str_repeat'.")


def test_find_sees_an_uncommitted_file_and_forgets_it_when_removed(
    index: Index,
    repo: Path,
) -> None:
    (repo / "src/fresh.py").write_text("def fresh_banana():\n    pass\n")
    assert "src/fresh.py:1:def fresh_banana():" in search.find(index, "fresh_banana").text

    (repo / "src/fresh.py").unlink()
    assert search.find(index, "fresh_banana").text.startswith("Nothing named or containing")


def test_find_sees_an_uncommitted_edit_to_a_tracked_file(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    assert (
        "src/BananaBender.php:3:public function headscarf(int $count): string" in search.find(index, "headscarf").text
    )
    assert "src/BananaBender.php" not in search.find(index, "bandana").text


def test_find_falls_back_to_text_when_no_symbol_matches(index: Index) -> None:
    assert search.find(index, "str_repeat").text.splitlines() == [
        "No symbol or path named 'str_repeat'. Text matches:",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:        return str_repeat('banana', $count);",
    ]


def test_find_matches_an_abbreviation_only_at_the_start_of_words(index: Index, repo: Path) -> None:
    (repo / "src/controllers.php").write_text(
        "<?php\nclass RequestController {}\nclass FrequencyController {}\nclass ControllerRequest {}\n"
    )

    assert search.find(index, "reqController").text.splitlines() == [
        "No name or path contains 'reqController'. It may be short for:",
        "src/controllers.php:2:class RequestController",
    ]
    assert search.find(index, "reqcontroller").text.splitlines()[1:] == [
        "src/controllers.php:2:class RequestController"
    ]
    assert search.find(index, "reqCont", kind=Kind.CLASS).text.splitlines()[1:] == [
        "src/controllers.php:2:class RequestController",
    ]


def test_find_matches_a_path_by_abbreviation(index: Index) -> None:
    assert search.find(index, "legSplit").text.splitlines() == [
        "No name or path contains 'legSplit'. It may be short for:",
        deleted(index, "legacy/split.py"),
    ]


def test_find_skips_abbreviations_when_a_closer_match_exists(index: Index, repo: Path) -> None:
    assert "split.py:1:def banana_split(" in search.find(index, "banSplit").text
    (repo / "src/b.py").write_text("def banSplit():\n    pass\n")
    assert search.find(index, "banSplit").text.splitlines() == ["src/b.py:1:def banSplit():"]


def test_find_matches_a_file_by_path(index: Index) -> None:
    assert search.find(index, "split.py").text == deleted(index, "legacy/split.py")


def test_grep_names_the_enclosing_symbol(index: Index) -> None:
    assert search.grep(index, "str_repeat").text.splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:        return str_repeat('banana', $count);",
    ]


def test_grep_searches_deleted_files_when_no_live_file_matches(index: Index) -> None:
    split = deleted(index, "legacy/split.py")
    assert search.grep(index, "cherry_sauce").text.splitlines() == [
        "1 hit in 1 file",
        split + "-1-def banana_split(scoops: int) -> str:",
        split + ':2:    topping = "cherry_sauce"',
    ]


def test_grep_marks_the_line_that_defines_a_matching_symbol(index: Index, repo: Path) -> None:
    (repo / "src/caller.php").write_text("<?php\nfunction feed() {\n    return (new BananaBender())->bandana(2);\n}\n")

    output = search.grep(index, "bandana", path="src/").text.splitlines()

    assert output[0] == "2 hits in 2 files"
    assert sorted(output[1:]) == [
        "src/BananaBender.php:3:    public function bandana(int $count): string {" + "  [definition]",
        "src/caller.php-2-function feed()",
        "src/caller.php:3:    return (new BananaBender())->bandana(2);",
    ]


def test_grep_marks_a_definition_when_the_pattern_holds_more_than_the_name(index: Index) -> None:
    assert search.grep(index, "function bandana").text.splitlines()[1].endswith("  [definition]")
    assert not search.grep(index, "int $count").text.splitlines()[1].endswith("  [definition]")


def test_grep_passes_over_a_file_that_holds_the_trigrams_but_not_the_text(index: Index, repo: Path) -> None:
    # "abcd xbcde" holds abc, bcd and cde, which are the trigrams of "abcde", so the text index picks the file.
    (repo / "src/near.txt").write_text("abcd xbcde\n")
    (repo / "src/exact.txt").write_text("abcde\n")

    assert search.grep(index, "abcde").text.splitlines() == ["1 hit in 1 file", "src/exact.txt:1:abcde"]


def test_grep_counts_the_hits_it_leaves_out(index: Index, repo: Path) -> None:
    for number in range(5):
        (repo / f"src/many{number}.txt").write_text("pip\npip\n")

    output = search.grep(index, "pip", path="many", limit=3).text.splitlines()

    assert output[0] == "10 hits in 5 files, 3 shown. Use --limit or --path to see more."
    assert len([line for line in output if ":pip" in line]) == 3


def test_grep_reads_past_the_count_limit_until_it_has_shown_its_limit(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "COUNT_FILES", 1)
    (repo / "src/q.txt").write_text("qxq\n")

    assert "src/q.txt:1:qxq" in search.grep(index, "q.q", regex=True).text


def test_grep_prints_the_history_notice_only_for_a_deleted_file_search(index: Index) -> None:
    with index.connect() as session:
        session.delete_meta("history_head")

    assert search.HISTORY_BUILDING not in search.grep(index, "str_repeat").text
    assert search.HISTORY_BUILDING not in search.find(index, "banana").text
    assert search.HISTORY_BUILDING in search.grep(index, "str_repeat", deleted=True).text


def test_grep_with_a_regex_and_a_pattern_too_short_for_the_index(index: Index) -> None:
    assert ":4:        return str_repeat" in search.grep(index, r"str_\w+\('banana'", regex=True).text
    assert ":2:class BananaBender extends Lady {" in search.grep(index, "{").text


def test_grep_is_case_sensitive_only_when_the_pattern_has_a_capital(index: Index) -> None:
    assert "class BananaBender" in search.grep(index, "bananabender").text
    assert search.grep(index, "BANANAbender").text.startswith("No text matches")


def test_grep_lists_commits_that_removed_the_text(index: Index) -> None:
    assert "Remove the split" in search.grep(index, "cherry_sauce", commits=True).text


def test_outline_indents_nested_symbols(index: Index) -> None:
    assert search.outline(index, "BananaBender.php").text.splitlines() == [
        "src/BananaBender.php",
        "2-6:class BananaBender extends Lady",
        "3-5:  public function bandana(int $count): string",
    ]


def test_show_prints_one_symbol(index: Index) -> None:
    assert search.show(index, "BananaBender.bandana").text.splitlines() == [
        "src/BananaBender.php:3-5",
        "3:    public function bandana(int $count): string {",
        "4:        return str_repeat('banana', $count);",
        "5:    }",
    ]


def test_show_prints_a_deleted_file_and_a_line_range(index: Index) -> None:
    split = deleted(index, "legacy/split.py")
    assert search.show(index, "legacy/split.py").text.splitlines() == [
        split + ":1-3",
        "1:def banana_split(scoops: int) -> str:",
        '2:    topping = "cherry_sauce"',
        "3:    return topping * scoops",
    ]
    assert search.show(index, split + ":2").text == split + ":2-2\n" + '2:    topping = "cherry_sauce"'
    assert search.show(index, "src/BananaBender.php:2-2").text.splitlines()[1] == "2:class BananaBender extends Lady {"


def test_lookup_finds_a_name_that_is_not_a_file(index: Index) -> None:
    assert search.lookup(index, "banana").text == search.find(index, "banana").text
    assert search.lookup(index, "BananaBender.bandana").text == search.find(index, "BananaBender.bandana").text


def test_lookup_treats_a_plain_word_as_a_name_even_when_a_file_has_that_name(
    index: Index,
    repo: Path,
) -> None:
    (repo / "bin").mkdir()
    (repo / "bin/bandana").write_text("#!/bin/sh\necho bandana\n")

    assert search.lookup(index, "bandana").text == search.find(index, "bandana").text
    assert search.lookup(index, "bin/bandana").text == "bin/bandana:1-2\n1:#!/bin/sh\n2:echo bandana"


def test_lookup_outlines_a_file_live_or_deleted(index: Index) -> None:
    assert search.lookup(index, "BananaBender.php").text == search.outline(index, "src/BananaBender.php").text
    assert search.lookup(index, "split.py").text == search.outline(index, "legacy/split.py").text
    assert search.lookup(index, deleted(index, "legacy/split.py")).text == search.outline(index, "legacy/split.py").text


def test_lookup_shows_a_file_that_defines_nothing(index: Index, repo: Path) -> None:
    (repo / "notes.txt").write_text("bananas are berries\n")
    assert search.lookup(index, "notes.txt").text == "notes.txt:1-1\n1:bananas are berries"


def test_lookup_shows_a_line_range(index: Index) -> None:
    assert search.lookup(index, "src/BananaBender.php:2-3").text == search.show(index, "src/BananaBender.php:2-3").text


def test_lookup_with_a_kind_searches_names_only(index: Index) -> None:
    assert search.lookup(index, "split.py", kind=Kind.FUNCTION).text.startswith("No function named 'split.py'.")


def test_a_file_added_back_is_no_longer_listed_as_deleted(index: Index, repo: Path) -> None:
    (repo / "legacy").mkdir()
    (repo / "legacy/split.py").write_text(SPLIT)
    assert search.find(index, "banana_split").text.splitlines() == [
        "legacy/split.py:1:def banana_split(scoops: int) -> str:",
    ]


def test_history_follows_head_to_another_branch(index: Index, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "before", "HEAD~1")
    index.refresh_history()
    assert "^:legacy/split.py" not in search.find(index, "banana_split").text

    git(repo, "checkout", "-q", "main")
    index.refresh_history()
    assert "^:legacy/split.py" in search.find(index, "banana_split").text


def test_a_history_rebuild_keeps_deleted_files_a_search_could_clean_up(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Side work")
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Main work")
    index.refresh_history()

    read_deletions = git_module.deletions

    def deletions_with_a_search_alongside(root: Path, since: str | None) -> list[git_module.Deletion]:
        found = read_deletions(root, since)
        with index.connect() as session:
            session.delete_unused_blobs()
        return found

    inserted: list[str] = []
    insert_blob = IndexSession.insert_blob

    def recording_insert_blob(self: IndexSession, oid: str, *args: Any, **kwargs: Any) -> int:
        inserted.append(oid)
        return insert_blob(self, oid, *args, **kwargs)

    monkeypatch.setattr(git_module, "deletions", deletions_with_a_search_alongside)
    monkeypatch.setattr(IndexSession, "insert_blob", recording_insert_blob)
    git(repo, "checkout", "-q", "side")
    index.refresh_history()

    assert inserted == []
    assert "^:legacy/split.py" in search.find(index, "banana_split").text


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
    assert "return 399" in search.show(index, "C399.run").text
    assert "def run(self):" in search.find(index, "run", path="many", kind=Kind.FUNCTION, limit=500).text


def test_the_same_content_is_indexed_again_under_another_extension(
    index: Index,
    repo: Path,
) -> None:
    (repo / "src/first.lock").write_text(SPLIT)
    search.find(index, "first.lock")
    (repo / "src/second.py").write_text(SPLIT)

    assert "src/second.py:1:def banana_split" in search.find(index, "banana_split").text
    assert search.grep(index, "cherry_sauce").text.splitlines()[1].startswith("src/second.py")


def test_line_numbers_ignore_form_feeds(index: Index, repo: Path) -> None:
    (repo / "src/feed.py").write_text("x = 1\f\n\n\ndef needle():\n    return 'haystack'\n")
    assert search.grep(index, "haystack").text.splitlines() == [
        "1 hit in 1 file",
        "src/feed.py-4-def needle():",
        "src/feed.py:5:    return 'haystack'",
    ]
    assert search.show(index, "needle").text.splitlines()[1] == "4:def needle():"


def test_an_untracked_symlink_is_not_followed(index: Index, repo: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("secret_outside_repo\n")
    (repo / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert search.grep(index, "secret_outside_repo").text.startswith("No text matches")


def test_a_search_while_the_history_build_is_unfinished_keeps_the_deleted_files(
    index: Index,
) -> None:
    with index.connect() as session:
        session.delete_meta("history_head")
    index.refresh()
    assert "^:legacy/split.py" in search.find(index, "banana_split").text

    index.refresh_history()
    assert index.history_is_current()


def test_directory_mode_indexes_files_from_disk(plain_directory: Path, tmp_path: Path) -> None:
    index = Index(plain_directory, cache_dir=tmp_path / "cache")
    assert not index.in_git

    assert search.find(index, "bandana").text.splitlines() == [
        "src/BananaBender.php:3:public function bandana(int $count): string",
    ]
    assert search.grep(index, "str_repeat", commits=True).text.splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-public function bandana(int $count): string",
        "src/BananaBender.php:4:        return str_repeat('banana', $count);",
        "Commit search needs a git repository.",
    ]


def test_directory_mode_sees_edits_and_removals(plain_directory: Path, tmp_path: Path) -> None:
    index = Index(plain_directory, cache_dir=tmp_path / "cache")
    search.find(index, "bandana")
    (plain_directory / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    (plain_directory / "src/fresh.py").write_text("def fresh_banana():\n    pass\n")

    assert (
        "src/BananaBender.php:3:public function headscarf(int $count): string" in search.find(index, "headscarf").text
    )
    assert "src/fresh.py" in search.find(index, "fresh_banana").text

    (plain_directory / "src/fresh.py").unlink()
    assert search.find(index, "fresh_banana").text.startswith("Nothing named or containing")


def test_part_of_a_name_is_found_after_edits_and_the_name_index_stays_whole(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headscarf"))
    assert ":3:public function headscarf(" in search.find(index, "adscar").text

    (repo / "src/BananaBender.php").write_text(BENDER)
    assert "headscarf" not in search.find(index, "adscar").text
    assert ":3:public function bandana(" in search.find(index, "ndan").text

    with index.connect() as session:
        session.insert_symbol_name("integrity-check")


def test_show_at_a_ref_prints_the_body_a_method_had_then(index: Index, repo: Path) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("'banana', $count", "'plantain', $count * 2"))
    git(repo, "commit", "-q", "-am", "Bend plantains")

    assert "plantain" in search.show(index, "BananaBender.bandana").text
    assert search.show(index, "BananaBender.bandana", ref="HEAD~1").text.splitlines() == [
        "HEAD~1:src/BananaBender.php:3-5",
        "3:    public function bandana(int $count): string {",
        "4:        return str_repeat('banana', $count);",
        "5:    }",
    ]


def test_find_at_a_ref_finds_a_definition_that_was_deleted_later(index: Index) -> None:
    assert search.find(index, "banana_split", ref="HEAD~1").text.splitlines() == [
        "HEAD~1:legacy/split.py:1:def banana_split(scoops: int) -> str:",
    ]
    assert search.find(index, "banana_split", ref="main").text.startswith(
        "Nothing named or containing 'banana_split' at main"
    )


def test_find_at_a_ref_finds_a_class_that_was_deleted_later(index: Index, repo: Path) -> None:
    git(repo, "rm", "-q", "src/BananaBender.php")
    git(repo, "commit", "-q", "-m", "Remove the bender")

    assert search.find(index, "BananaBender", kind=Kind.CLASS, ref="HEAD~1").text.splitlines() == [
        "HEAD~1:src/BananaBender.php:2:class BananaBender extends Lady",
    ]


def test_outline_at_a_ref_reads_the_file_from_git(index: Index) -> None:
    assert search.outline(index, "split.py", ref="HEAD~1").text.splitlines() == [
        "HEAD~1:legacy/split.py",
        "1-3:def banana_split(scoops: int) -> str:",
    ]


def test_show_at_a_ref_says_when_the_symbol_is_not_there(index: Index) -> None:
    assert search.show(index, "banana_split", ref="main").text == "No symbol or file called 'banana_split' at main."


def test_an_unknown_ref_raises_an_error_that_names_it(index: Index) -> None:
    with pytest.raises(UnknownRef, match="'no-such-branch' is not a branch, tag or commit"):
        search.find(index, "banana", ref="no-such-branch")


def test_a_kind_the_grammar_pack_has_no_name_for_is_read_from_its_json(index: Index, repo: Path) -> None:
    (repo / "src/ripe.c").write_text("#define BANANA_COUNT 3\ntypedef struct bunch bunch_t;\n")

    assert search.find(index, "BANANA_COUNT", kind=Kind.CONSTANT).text.startswith("src/ripe.c:1:")
    assert search.find(index, "bunch_t", kind=Kind.TYPE).text.startswith("src/ripe.c:2:")


def test_find_at_a_ref_reads_symbols_from_the_index_when_it_holds_the_content(
    index: Index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(path: str, text: str) -> list[object]:
        raise AssertionError(f"{path} was parsed again")

    # HEAD holds src/BananaBender.php as the working tree does, so the index has its symbols.
    monkeypatch.setattr(at_ref, "extract", refuse)

    assert search.find(index, "bandana", ref="HEAD").text.splitlines() == [
        "HEAD:src/BananaBender.php:3:public function bandana(int $count): string"
    ]


def test_find_at_a_ref_searches_the_whole_commit_when_too_many_files_are_not_indexed(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo / "src/BananaBender.php").write_text(BENDER.replace("bandana", "headband"))
    git(repo, "commit", "-q", "-am", "Wear a headband")
    git(repo, "checkout", "-q", "HEAD~1")
    monkeypatch.setattr(settings, "GREP_PATHS", 0)

    assert "main:src/BananaBender.php:3:" in search.find(index, "headband", ref="main").text


def test_find_puts_a_heading_between_exact_and_partial_matches(index: Index, repo: Path) -> None:
    (repo / "src/peel.py").write_text("def peel():\n    pass\n\n\ndef peel_twice():\n    pass\n")

    assert search.find(index, "peel").text.splitlines() == [
        "src/peel.py:1:def peel():",
        search.PARTIAL_MATCHES,
        "src/peel.py:5:def peel_twice():",
    ]
    assert search.PARTIAL_MATCHES not in search.find(index, "peel_twice").text
    assert search.PARTIAL_MATCHES not in search.find(index, "pee").text


def test_find_counts_paths_within_its_limit(index: Index, repo: Path) -> None:
    for number in range(4):
        (repo / f"src/crate{number}.txt").write_text("fruit\n")

    lines = search.find(index, "crate", limit=3).text.splitlines()

    assert len(lines) == 4
    assert all(line.startswith("src/crate") for line in lines[:3])
    assert lines[3] == "+1 more file with a matching path."


@pytest.mark.parametrize("query", ["src/nope.py", "nope.py", "src/nope"])
def test_find_does_not_guess_at_a_file_name_that_names_no_file(index: Index, repo: Path, query: str) -> None:
    # Each query could be cut into the starts of words in this path, and "py" alone starts a word of every ".py" path.
    (repo / "src/node_persist.py").write_text("x = 1\n")

    result = search.lookup(index, query)

    assert not result.found
    assert result.text.startswith(f"Nothing named or containing {query!r}")


def test_find_still_guesses_at_a_path_from_a_plain_word(index: Index, repo: Path) -> None:
    (repo / "src/node_persist.py").write_text("x = 1\n")

    assert search.lookup(index, "nopers").text.splitlines() == [
        "No name or path contains 'nopers'. It may be short for:",
        "src/node_persist.py",
    ]


def test_a_search_says_whether_it_found_anything(index: Index) -> None:
    assert search.find(index, "BananaBender").found
    assert search.grep(index, "str_repeat").found
    assert search.show(index, "BananaBender").found
    assert search.outline(index, "BananaBender.php").found

    assert not search.find(index, "no_such_fruit").found
    assert not search.grep(index, "no_such_fruit").found
    assert not search.show(index, "no_such_fruit").found
    assert not search.outline(index, "no_such_fruit.php").found
    assert not search.callers(index, "no_such_fruit").found
    assert not search.find(index, "no_such_fruit", ref="HEAD").found


def test_grep_shows_no_more_matches_than_its_limit(index: Index, repo: Path) -> None:
    (repo / "src/many.txt").write_text("pip\n" * 6)

    assert search.grep(index, "pip", limit=2).text.splitlines() == [
        "6 hits in 1 file, 2 shown. Use --limit or --path to see more.",
        "src/many.txt:1:pip",
        "src/many.txt:2:pip",
        "(4 more matching lines in src/many.txt)",
    ]


def test_grep_keeps_the_indentation_of_a_line(index: Index) -> None:
    assert (
        "src/BananaBender.php:4:        return str_repeat('banana', $count);" in search.grep(index, "str_repeat").text
    )


def test_grep_lists_only_the_files_when_asked(index: Index, repo: Path) -> None:
    for number in range(3):
        (repo / f"src/many{number}.txt").write_text("pip\npip\n")

    assert search.grep(index, "pip", files_only=True).text.splitlines() == [
        "6 hits in 3 files",
        "src/many0.txt",
        "src/many1.txt",
        "src/many2.txt",
    ]
    assert search.grep(index, "pip", files_only=True, limit=2).text.splitlines() == [
        "6 hits in 3 files, 2 files shown. Use --limit or --path to see more.",
        "src/many0.txt",
        "src/many1.txt",
    ]


def test_grep_shows_the_lines_around_a_match(index: Index, repo: Path) -> None:
    lines = [f"line{number} = {number}" for number in range(1, 13)]
    lines[2] = lines[9] = "pip = 0"
    (repo / "src/far.py").write_text("def far():\n" + "\n".join(f"    {line}" for line in lines) + "\n")

    assert search.grep(index, "pip", context=1).text.splitlines() == [
        "2 hits in 1 file",
        "src/far.py-1-def far():",
        "--",
        "src/far.py-3-    line2 = 2",
        "src/far.py:4:    pip = 0",
        "src/far.py-5-    line4 = 4",
        "--",
        "src/far.py-10-    line9 = 9",
        "src/far.py:11:    pip = 0",
        "src/far.py-12-    line11 = 11",
    ]


def test_grep_context_holds_the_enclosing_symbol_line_once(index: Index) -> None:
    assert search.grep(index, "str_repeat", context=1).text.splitlines() == [
        "1 hit in 1 file",
        "src/BananaBender.php-3-    public function bandana(int $count): string {",
        "src/BananaBender.php:4:        return str_repeat('banana', $count);",
        "src/BananaBender.php-5-    }",
    ]


def test_grep_context_shows_a_signature_over_several_lines_once(index: Index, repo: Path) -> None:
    (repo / "src/wide.py").write_text("\n\ndef wide(\n    first: int,\n) -> None:\n    pip = first\n")

    assert search.grep(index, "pip", context=3).text.splitlines()[1:] == [
        "src/wide.py-3-def wide(",
        "src/wide.py-4-    first: int,",
        "src/wide.py-5-) -> None:",
        "src/wide.py:6:    pip = first",
    ]
    assert search.grep(index, "pip", context=1).text.splitlines()[1:] == [
        "src/wide.py-3-def wide(",
        "--",
        "src/wide.py-5-) -> None:",
        "src/wide.py:6:    pip = first",
    ]
    assert search.grep(index, "pip").text.splitlines()[1:] == [
        "src/wide.py-3-def wide(first: int) -> None:",
        "src/wide.py:6:    pip = first",
    ]


def test_grep_context_stops_at_a_matching_line_that_is_past_the_limit(index: Index, repo: Path) -> None:
    (repo / "src/many.txt").write_text("before\npip 1\nbetween\npip 2\nafter\n")

    assert search.grep(index, "pip", context=3, limit=1).text.splitlines() == [
        "2 hits in 1 file, 1 shown. Use --limit or --path to see more.",
        "src/many.txt-1-before",
        "src/many.txt:2:pip 1",
        "src/many.txt-3-between",
        "(1 more matching line in src/many.txt)",
    ]


def test_grep_context_leaves_out_a_matching_line_that_is_past_the_limit(index: Index, repo: Path) -> None:
    (repo / "src/many.txt").write_text("pip 1\npip 2\npip 3\nlast\n")

    assert search.grep(index, "pip", context=1, limit=1).text.splitlines() == [
        "3 hits in 1 file, 1 shown. Use --limit or --path to see more.",
        "src/many.txt:1:pip 1",
        "(2 more matching lines in src/many.txt)",
    ]


def test_grep_context_puts_a_break_between_two_files(index: Index, repo: Path) -> None:
    (repo / "src/one.txt").write_text("pip\n")
    (repo / "src/two.txt").write_text("pip\n")

    assert search.grep(index, "pip", context=1).text.splitlines()[1:] == [
        "src/one.txt:1:pip",
        "--",
        "src/two.txt:1:pip",
    ]
    assert "--" not in search.grep(index, "pip").text.splitlines()


@pytest.mark.parametrize("pattern", ["(?i)BANANABENDER", "(?i)(?m)BANANABENDER", "(?ix) BANANABENDER # the class"])
def test_grep_for_a_word_keeps_the_flags_at_the_start_of_a_regex(index: Index, pattern: str) -> None:
    assert search.grep(index, pattern, regex=True, word=True, case=Case.SENSITIVE).found


def test_grep_for_a_word_still_refuses_a_regex_that_does_not_compile(index: Index) -> None:
    with pytest.raises(search.InvalidPattern, match="bad escape"):
        search.grep(index, "(?x)banana\\", regex=True, word=True)


def test_grep_that_lists_only_files_has_no_breaks(index: Index, repo: Path) -> None:
    (repo / "src/sauce.txt").write_text("cherry_sauce\n")

    lines = search.grep(index, "cherry_sauce", context=1, deleted=True, files_only=True).text.splitlines()

    assert len(lines) == 3
    assert "--" not in lines


def test_grep_context_puts_a_break_between_a_live_and_a_deleted_file(index: Index, repo: Path) -> None:
    (repo / "src/sauce.txt").write_text("cherry_sauce\n")

    lines = search.grep(index, "cherry_sauce", context=1, deleted=True).text.splitlines()

    assert lines[1:3] == ["src/sauce.txt:1:cherry_sauce", "--"]
    assert lines[3].endswith("^:legacy/split.py-1-def banana_split(scoops: int) -> str:")


def test_grep_matches_a_whole_word_when_asked(index: Index, repo: Path) -> None:
    (repo / "src/words.txt").write_text("peel\npeeled\nun-peel it\n$peel\n")

    assert search.grep(index, "peel", word=True).text.splitlines() == [
        "3 hits in 1 file",
        "src/words.txt:1:peel",
        "src/words.txt:3:un-peel it",
        "src/words.txt:4:$peel",
    ]


def test_grep_case_can_be_forced_either_way(index: Index) -> None:
    assert search.grep(index, "BANANAbender", case=Case.IGNORE).found
    assert not search.grep(index, "bananabender", case=Case.SENSITIVE).found


def test_grep_refuses_a_regex_that_does_not_compile(index: Index) -> None:
    with pytest.raises(search.InvalidPattern, match="is not a regex Python can read"):
        search.grep(index, "(", regex=True)


def test_grep_says_where_the_pattern_matched(index: Index) -> None:
    match = search.grep(index, "str_repeat").lines[-1]

    assert match.text[match.spans[0].start : match.spans[0].end] == "str_repeat"


def test_grep_shows_a_match_that_is_far_along_a_long_line(index: Index, repo: Path) -> None:
    (repo / "src/long.txt").write_text("a" * 600 + " needle " + "b" * 600 + "\n")

    match = search.grep(index, "needle").lines[-1]

    assert match.text.startswith(search.CUT_BEFORE + "a" * (settings.MATCH_LEAD - 1) + " needle b")
    assert len(match.text) == len(search.CUT_BEFORE) + settings.MAX_LINE_LENGTH
    assert [match.text[span.start : span.end] for span in match.spans] == ["needle"]


def test_grep_gives_a_span_to_a_match_that_the_cut_goes_through(index: Index, repo: Path) -> None:
    (repo / "src/long.txt").write_text("start " + "y" * 600 + "needle\n")

    match = search.grep(index, "y+needle", regex=True).lines[-1]

    assert match.text == "start " + "y" * (settings.MAX_LINE_LENGTH - 6)
    assert match.spans == (Span(6, settings.MAX_LINE_LENGTH),)


def test_grep_does_not_take_the_cut_marker_for_a_match(index: Index, repo: Path) -> None:
    (repo / "src/long.txt").write_text("a" * 600 + " needle...\n")

    match = search.grep(index, r"\.\.\.|needle", regex=True).lines[-1]

    assert [match.text[span.start : span.end] for span in match.spans] == ["needle", "..."]
    assert match.spans[0].start > len(search.CUT_BEFORE)


def test_show_refuses_a_line_past_the_end_of_the_file(index: Index) -> None:
    result = search.show(index, "src/BananaBender.php:90-95")

    assert not result.found
    assert result.text == "src/BananaBender.php has 6 lines, so it has no line 90."


@pytest.mark.parametrize("lines", ["4-2", "3-0"])
def test_show_refuses_a_range_that_ends_before_it_starts(index: Index, lines: str) -> None:
    result = search.show(index, f"src/BananaBender.php:{lines}")

    assert not result.found
    assert result.text == f"{lines} is not a range of lines, because it ends before it starts."


def test_show_refuses_a_binary_file_at_a_ref_as_it_does_in_the_working_tree(index: Index, repo: Path) -> None:
    (repo / "blob.dat").write_bytes(b"ab\0cd\n")
    git(repo, "add", "blob.dat")
    git(repo, "commit", "-q", "-m", "Add a binary file")

    for result in (search.lookup(index, "blob.dat"), search.lookup(index, "blob.dat", ref="HEAD")):
        assert not result.found
        assert result.text.endswith("blob.dat can't be shown: it is binary, larger than 1MB, or no longer in git.")


def test_show_at_a_ref_says_how_to_show_the_rest_of_a_long_file(
    index: Index,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SHOW_MAX_LINES", 2)

    lines = search.show(index, "src/BananaBender.php", ref="HEAD").text.splitlines()

    assert lines[-1] == "(4 more lines. Continue with show src/BananaBender.php:3-6 --ref HEAD)"
    assert search.show(index, "src/BananaBender.php:3-6", ref="HEAD").text.splitlines()[0] == (
        "HEAD:src/BananaBender.php:3-4"
    )


def test_show_starts_at_the_first_line_when_asked_for_line_0(index: Index) -> None:
    assert search.show(index, "src/BananaBender.php:0-2").text.splitlines() == [
        "src/BananaBender.php:1-2",
        "1:<?php",
        "2:class BananaBender extends Lady {",
    ]


def test_lookup_at_a_ref_shows_a_file_that_defines_nothing(index: Index, repo: Path) -> None:
    (repo / "notes.txt").write_text("bananas are berries\n")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "Add notes")

    assert search.lookup(index, "notes.txt", ref="HEAD").text == "HEAD:notes.txt:1-1\n1:bananas are berries"
    assert search.lookup(index, "split.py", ref="HEAD~2").text.startswith(
        "HEAD~2:legacy/split.py\n1-3:def banana_split"
    )


def test_show_takes_line_0_alone_as_the_first_line(index: Index) -> None:
    assert search.show(index, "src/BananaBender.php:0").text == "src/BananaBender.php:1-1\n1:<?php"


def test_show_says_that_an_empty_file_is_empty(index: Index, repo: Path) -> None:
    (repo / "src/empty.py").write_text("")
    git(repo, "add", "src/empty.py")
    git(repo, "commit", "-q", "-m", "Add an empty file")

    assert search.show(index, "src/empty.py").text == "src/empty.py\n(the file is empty)"
    assert search.show(index, "src/empty.py", ref="HEAD").text == "HEAD:src/empty.py\n(the file is empty)"
    assert search.show(index, "src/empty.py:5").text == "src/empty.py has 0 lines, so it has no line 5."


def test_show_lists_the_other_definitions_as_places_to_jump_to(index: Index, repo: Path) -> None:
    (repo / "src/other.py").write_text("def bandana():\n    pass\n")

    assert search.show(index, "bandana").text.splitlines()[-2:] == [
        "Also defined at (use --path to pick one):",
        "src/BananaBender.php:3:public function bandana(int $count): string",
    ]


def test_a_signature_over_several_lines_is_joined_as_if_written_on_one(index: Index, repo: Path) -> None:
    (repo / "src/wide.py").write_text(
        "def wide(\n    first: int,\n    second: list[\n        str\n    ],\n) -> None:\n    pass\n"
    )

    assert search.find(index, "wide").text == "src/wide.py:1:def wide(first: int, second: list[str]) -> None:"


def test_the_index_reports_how_far_a_long_read_is(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "BATCH_SIZE", 1)
    reported: list[tuple[str, int, int]] = []

    def record(reading: Reading, *, done: int, total: int) -> None:
        reported.append((reading, done, total))

    index = Index(repo, cache_dir=tmp_path / "cache", report=record)
    (repo / "src/more.py").write_text("x = 1\n")

    index.refresh()
    index.refresh_history()

    assert reported == [("files", 0, 2), ("files", 1, 2), ("deleted files", 0, 1)]
