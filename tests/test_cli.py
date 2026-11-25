"""Runs the command line the way a coding harness does, against a small git repository."""

import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from what import cli
from what.config import settings
from what.index import Index

runner = CliRunner()


@pytest.fixture
def cache(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the command at the test repository and at an index folder of its own."""
    monkeypatch.setenv("WHAT_ROOT", str(repo))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "what"


def run(*args: str) -> str:
    result = runner.invoke(cli.app, list(args), prog_name="what")
    return result.output


def test_a_name_with_no_command_finds_its_definition(cache: Path) -> None:
    assert run("BananaBender").startswith("src/BananaBender.php:2:class BananaBender extends Lady")


def test_a_file_with_no_command_lists_what_it_defines(cache: Path) -> None:
    assert run("BananaBender.php").startswith("src/BananaBender.php\n  2-6 class BananaBender extends Lady")


def test_options_work_with_no_command(cache: Path) -> None:
    assert (
        run("--kind", "method", "bandana").splitlines()[0]
        == "src/BananaBender.php:3:public function bandana(int $count): string"
    )


def test_find_joins_its_words_into_one_query(cache: Path) -> None:
    assert run("find", "Banana", "Bender").startswith("src/BananaBender.php:2:class BananaBender extends Lady")


def test_the_hidden_command_name_is_searched_for_like_any_name(cache: Path) -> None:
    assert run("lookup").startswith("Nothing named or containing 'lookup'")


def test_a_search_starts_the_deleted_file_build_in_another_process(repo: Path, cache: Path) -> None:
    run("BananaBender")
    index = Index(repo, cache_dir=cache)
    deadline = time.monotonic() + 30
    while not index.history_is_current() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert "^:legacy/split.py:1:def banana_split" in run("banana_split")


def test_grep_passes_its_options(cache: Path) -> None:
    run("--index")
    assert '^:legacy/split.py:2:topping = "cherry_sauce"' in run(
        "grep", "cherry_\\w+", "--regex", "--deleted", "--path", "legacy/"
    )


def test_no_arguments_prints_the_help(cache: Path) -> None:
    assert run().startswith("Usage: what [OPTIONS] [COMMAND] [ARGS]...")


def test_outside_a_repository_the_directory_is_searched(
    plain_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHAT_ROOT", str(plain_directory))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    assert run("BananaBender") == "src/BananaBender.php:2:class BananaBender extends Lady\n"


def test_a_root_that_is_not_a_directory_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHAT_ROOT", str(tmp_path / "missing"))
    result = runner.invoke(cli.app, ["banana"], prog_name="what")

    assert result.exit_code == 1
    assert "missing is not a directory" in result.output


def test_a_directory_that_is_too_big_is_refused(
    plain_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHAT_ROOT", str(plain_directory))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(settings, "MAX_FILES", 1)
    monkeypatch.setattr(sys, "argv", ["what", "banana"])

    with pytest.raises(SystemExit, match="has more than 1 files and is not a git repository"):
        cli.main()


def test_prompt_names_every_search() -> None:
    printed = run("--prompt")
    for usage in ["`what <name>`", "`what <file>`", "`what show ", "`what grep "]:
        assert usage in printed


def test_a_ref_option_reads_the_files_at_that_ref(cache: Path) -> None:
    assert run("show", "banana_split", "--ref", "HEAD~1").startswith("HEAD~1:legacy/split.py:1-3\ndef banana_split")
    assert run("banana_split", "--ref", "HEAD~1").startswith("HEAD~1:legacy/split.py:1:def banana_split")


def test_an_unknown_ref_exits_with_an_error(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["what", "show", "banana", "--ref", "no-such-branch"])

    with pytest.raises(SystemExit, match="what: 'no-such-branch' is not a branch, tag or commit") as exit:
        cli.main()

    assert exit.value.code != 0
