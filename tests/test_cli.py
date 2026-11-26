"""Runs the command line the way a coding harness does, against a small git repository."""

import importlib.metadata
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from what import cli, database, log
from what.config import settings
from what.index import Index

runner = CliRunner()


@pytest.fixture
def cache(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the command at the test repository and at an index folder of its own."""
    monkeypatch.setenv("WHAT_ROOT", str(repo))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "what"


def invoke(*args: str) -> Result:
    """Run the command without main, which is what adds the handler that prints errors."""
    log.show_errors()
    return runner.invoke(cli.app, list(args), prog_name="what")


def run(*args: str) -> str:
    return invoke(*args).output


def test_a_name_with_no_command_finds_its_definition(cache: Path) -> None:
    assert run("BananaBender").startswith("src/BananaBender.php:2:class BananaBender extends Lady")


def test_a_file_with_no_command_lists_what_it_defines(cache: Path) -> None:
    assert run("BananaBender.php").startswith("src/BananaBender.php\n2-6:class BananaBender extends Lady")


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
    run("index")
    assert '^:legacy/split.py:2:    topping = "cherry_sauce"' in run(
        "grep", "cherry_\\w+", "--regex", "--deleted", "--path", "legacy/"
    )


def test_no_arguments_prints_the_help(cache: Path) -> None:
    assert run().startswith("Usage: what [OPTIONS] COMMAND [ARGS]...")


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
    result = invoke("banana")

    assert result.exit_code == cli.ERROR
    assert "missing is not a directory" in result.stderr


def test_a_directory_that_is_too_big_is_refused(
    plain_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WHAT_ROOT", str(plain_directory))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(settings, "MAX_FILES", 1)
    monkeypatch.setattr(sys, "argv", ["what", "banana"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == cli.ERROR
    assert "has more than 1 files and is not a git repository" in capsys.readouterr().err


def test_prompt_names_every_search() -> None:
    printed = run("prompt")
    for usage in ["`what <name>`", "`what <file>`", "`what show ", "`what callers ", "`what grep "]:
        assert usage in printed


def test_a_ref_option_reads_the_files_at_that_ref(cache: Path) -> None:
    assert run("show", "banana_split", "--ref", "HEAD~1").startswith("HEAD~1:legacy/split.py:1-3\n1:def banana_split")
    assert run("banana_split", "--ref", "HEAD~1").startswith("HEAD~1:legacy/split.py:1:def banana_split")


def test_an_unknown_ref_exits_with_an_error(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["what", "show", "banana", "--ref", "no-such-branch"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == cli.ERROR
    assert "what: 'no-such-branch' is not a branch, tag or commit" in capsys.readouterr().err


def test_a_kind_that_does_not_exist_is_refused(cache: Path) -> None:
    result = invoke("find", "banana", "--kind", "clas")

    assert result.exit_code == 2
    assert "'clas' is not one of" in result.output


@pytest.mark.parametrize("command", [["grep"], [], ["find"], ["callers"], ["show"], ["outline"]])
def test_a_search_that_finds_nothing_exits_with_1_and_prints_to_stderr(cache: Path, command: list[str]) -> None:
    result = invoke(*command, "no_such_fruit")

    assert result.exit_code == cli.NOTHING_FOUND
    assert result.stdout == ""
    assert "no_such_fruit" in result.stderr


def test_a_path_that_matches_several_files_exits_with_1_and_lists_them_on_stderr(cache: Path, repo: Path) -> None:
    (repo / "legacy").mkdir()
    (repo / "legacy/BananaBender.php").write_text("<?php\n")

    result = invoke("outline", "BananaBender.php")

    assert result.exit_code == cli.NOTHING_FOUND
    assert result.stdout == ""
    assert result.stderr.splitlines()[0] == "'BananaBender.php' matches several files:"


def test_a_search_that_finds_something_exits_with_0(cache: Path) -> None:
    result = invoke("grep", "str_repeat")

    assert result.exit_code == 0
    assert result.stderr == ""


def test_a_path_that_names_no_file_is_not_answered_with_other_files(cache: Path) -> None:
    result = invoke("src/NoSuchFile.php")

    assert result.exit_code == cli.NOTHING_FOUND
    assert result.stderr.startswith("Nothing named or containing 'src/NoSuchFile.php'")


@pytest.mark.parametrize("option", [["--json"], ["--color", "never"], ["--color=never"]])
def test_an_output_option_works_before_the_command_name(cache: Path, option: list[str]) -> None:
    assert invoke(*option, "grep", "str_repeat").stdout == invoke("grep", "str_repeat", *option).stdout
    assert invoke(*option, "BananaBender").stdout == invoke("BananaBender", *option).stdout


@pytest.mark.parametrize("command", [["grep", "banana"], ["callers", "bandana"], ["find", "banana"], ["banana"]])
def test_a_limit_below_1_is_refused(cache: Path, command: list[str]) -> None:
    result = invoke(*command, "--limit", "0")

    assert result.exit_code == cli.ERROR
    assert "Invalid value for '--limit'" in result.stderr


def test_the_old_spellings_of_index_and_prompt_still_work(cache: Path) -> None:
    assert run("--prompt") == run("prompt")
    assert run("--index") == run("index")


def test_a_usage_error_with_no_command_names_what_and_not_the_hidden_command(cache: Path) -> None:
    result = invoke("BananaBender", "--no-such-option")

    assert result.exit_code == cli.ERROR
    assert "Usage: what [OPTIONS] QUERY..." in result.stderr
    assert "Try 'what --help' for help." in result.stderr
    assert "lookup" not in result.stderr


def test_version_prints_the_installed_version() -> None:
    assert run("--version") == f"what {importlib.metadata.version('what')}\n"
    assert run("-V") == run("--version")


def test_index_is_a_command_and_the_name_is_searched_with_find(cache: Path, repo: Path) -> None:
    (repo / "src/index.py").write_text("def index():\n    return 1\n")

    assert run("index") == "The index holds 2 files, 1 deleted file and 0 other branches.\n"
    assert run("find", "index").startswith("src/index.py:1:def index():")


def test_status_says_what_the_index_holds(cache: Path, repo: Path, log_file: Path) -> None:
    assert run("status").startswith(f"{repo} has no index yet.")
    assert not list(cache.glob("*.db"))

    run("index")
    lines = run("status").splitlines()

    assert lines[0] == f"folder            {repo}"
    assert lines[1].startswith(f"index             {cache}/repo-")
    assert lines[1].endswith(f" MB, schema {database.SCHEMA_VERSION})")
    assert lines[2:] == [
        "files             1 with 2 symbols",
        "deleted files     1 (up to date)",
        "other branches    0",
        "background build  not running",
        f"log               {log_file}",
    ]


def test_status_says_when_the_deleted_files_are_not_read_and_a_build_must_wait(
    cache: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(index: Index) -> None:
        raise RuntimeError("git went away")

    monkeypatch.setattr(Index, "refresh_history", fail)
    invoke("index")

    lines = run("status").splitlines()

    assert lines[3] == "deleted files     0 (not up to date)"
    assert lines[5] == "background build  started or failed not long ago; a search may start it again in 5 minutes"


def test_status_of_a_folder_that_is_not_a_repository(
    plain_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    run("-C", str(plain_directory), "index")

    assert "deleted files     0 (not a git repository)" in run("-C", str(plain_directory), "status")


def test_status_does_not_start_a_build(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    def record(index: Index) -> None:
        started.append(True)

    monkeypatch.setattr(Index, "build_in_background", record)

    run("status")

    assert started == []


def test_the_folder_option_searches_another_folder(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    found = "src/BananaBender.php:2:class BananaBender extends Lady\n"

    assert run("-C", str(repo), "BananaBender") == found
    assert run(f"-C{repo}", "find", "BananaBender") == found
    assert run("-C", str(repo), "--kind", "class", "BananaBender") == found
    assert run("-C", str(repo), "grep", "-C", "1", "class BananaBender").splitlines()[1:] == [
        "src/BananaBender.php-1-<?php",
        "src/BananaBender.php:2:class BananaBender extends Lady {  [definition]",
        "src/BananaBender.php-3-    public function bandana(int $count): string {",
    ]


def test_the_background_build_is_started_for_the_folder_option_and_not_for_what_root(
    repo: Path,
    plain_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("WHAT_ROOT", str(plain_directory))
    started: list[list[str]] = []
    start = subprocess.Popen

    def record(command: list[str], **options: Any) -> object:
        """Record a command that runs what, in place of starting it. git's commands run as they are."""
        if command[0] != sys.executable:
            return start(command, **options)

        started.append(command)
        return None

    monkeypatch.setattr(subprocess, "Popen", record)
    run("-C", str(repo), "BananaBender")

    assert started == [[sys.executable, "-m", "what", "-C", str(repo), "index"]]


def test_the_folder_option_refuses_a_folder_that_does_not_exist(tmp_path: Path) -> None:
    result = invoke("-C", str(tmp_path / "missing"), "BananaBender")

    assert result.exit_code == cli.ERROR
    assert "does not exist" in result.stderr


@pytest.mark.parametrize(
    ("options", "found"),
    [
        (["-l", "banana"], ["src/BananaBender.php", "src/fruit.txt"]),
        (["-s", "banana", "--path", "fruit"], ["src/fruit.txt:3:bananas"]),
        (["-i", "BANANAS"], ["src/fruit.txt:3:bananas"]),
        (["-w", "banana", "--path", "fruit"], ["src/fruit.txt:2:Banana"]),
        (["-e", "bananas?$", "--path", "fruit"], ["src/fruit.txt:2:Banana", "src/fruit.txt:3:bananas"]),
        (["-E", "^f.ur$"], ["src/fruit.txt:4:four"]),
        (["-C", "1", "four"], ["src/fruit.txt-3-bananas", "src/fruit.txt:4:four"]),
    ],
)
def test_grep_takes_the_short_options_of_grep_and_ripgrep(
    cache: Path,
    repo: Path,
    options: list[str],
    found: list[str],
) -> None:
    (repo / "src/fruit.txt").write_text("one\nBanana\nbananas\nfour\n")

    assert run("grep", *options).splitlines()[1:] == found


def test_grep_refuses_two_case_options_together(cache: Path) -> None:
    result = invoke("grep", "-i", "-s", "banana")

    assert result.exit_code == cli.ERROR
    assert "-i and -s can't be used together." in result.stderr


def test_a_regex_that_does_not_compile_exits_with_an_error(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["what", "grep", "-e", "("])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == cli.ERROR
    assert "what: '(' is not a regex Python can read" in capsys.readouterr().err


def test_json_prints_one_object_for_each_line(cache: Path) -> None:
    records = [json.loads(line) for line in run("grep", "str_repeat", "--json").splitlines()]

    assert records == [
        {"type": "note", "text": "1 hit in 1 file"},
        {
            "type": "context",
            "path": "src/BananaBender.php",
            "line": 3,
            "text": "public function bandana(int $count): string",
        },
        {
            "type": "match",
            "path": "src/BananaBender.php",
            "line": 4,
            "text": "        return str_repeat('banana', $count);",
            "spans": [[15, 25]],
        },
    ]


def test_json_says_where_a_file_outside_the_working_tree_comes_from(cache: Path) -> None:
    run("index")
    record = json.loads(run("banana_split", "--json").splitlines()[0])

    assert record["path"] == "legacy/split.py"
    assert len(record["deleted_in"]) == 40
    assert json.loads(run("banana_split", "--ref", "HEAD~1", "--json"))["ref"] == "HEAD~1"


def test_an_error_is_an_object_on_stdout_with_json_and_text_on_stderr(cache: Path) -> None:
    result = invoke("show", "banana", "--ref", "no-such-branch", "--json")

    assert result.exit_code == cli.ERROR
    assert json.loads(result.stdout) == {
        "type": "error",
        "text": "'no-such-branch' is not a branch, tag or commit in this repository",
    }
    assert result.stderr == "what: 'no-such-branch' is not a branch, tag or commit in this repository\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["grep", "banana", "--limit", "0", "--json"], "Invalid value for '--limit': 0 is not in the range x>=1."),
        (["--json", "BananaBender", "--no-such-option"], "No such option '--no-such-option'."),
        (["-C", "/no/such/folder", "BananaBender", "--json"], "Directory '/no/such/folder' does not exist."),
    ],
)
def test_a_mistake_in_the_options_is_an_object_on_stdout_with_json_and_text_on_stderr(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["what", *args])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    printed = capsys.readouterr()
    assert stopped.value.code == cli.ERROR
    assert json.loads(printed.out)["type"] == "error"
    assert message in json.loads(printed.out)["text"]
    assert message in printed.err


def test_json_after_two_dashes_is_text_to_search_for_and_not_the_option(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["what", "grep", "--limit", "0", "--", "--json"])

    with pytest.raises(SystemExit):
        cli.main()

    printed = capsys.readouterr()
    assert printed.out == ""
    assert "Error: Invalid value for '--limit'" in printed.err


def test_a_command_that_is_interrupted_says_so_and_exits_as_a_shell_expects(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupt(*args: object, **options: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.search, "grep", interrupt)
    monkeypatch.setattr(sys, "argv", ["what", "grep", "banana"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == cli.INTERRUPTED
    assert "Aborted!" in capsys.readouterr().err


def test_a_broken_pipe_that_is_not_the_output_is_a_failure(
    cache: Path,
    log_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def break_a_pipe(*args: object, **options: object) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(cli.search, "grep", break_a_pipe)
    monkeypatch.setattr(sys, "argv", ["what", "grep", "banana"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == cli.ERROR
    assert "a pipe that the command wrote to was closed" in capfd.readouterr().err
    assert ": failed in " in log_file.read_text()
    assert "BrokenPipeError" in log_file.read_text()


def test_a_mistake_in_the_options_is_plain_text_on_stderr_without_json(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["what", "grep", "banana", "--limit", "0"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    printed = capsys.readouterr()
    assert stopped.value.code == cli.ERROR
    assert printed.out == ""
    assert "Usage: what grep [OPTIONS] PATTERN..." in printed.err
    assert "Error: Invalid value for '--limit'" in printed.err


def test_json_goes_to_stdout_when_nothing_is_found(cache: Path) -> None:
    result = invoke("grep", "no_such_fruit", "--json")

    assert result.exit_code == cli.NOTHING_FOUND
    assert json.loads(result.stdout)["type"] == "note"
    assert result.stderr == ""


def test_color_always_groups_matches_under_their_file(cache: Path) -> None:
    lines = run("grep", "str_repeat", "--color", "always").splitlines()

    assert lines == [
        "1 hit in 1 file",
        "",
        "\x1b[35msrc/BananaBender.php\x1b[0m",
        "\x1b[32m3\x1b[0m-public function bandana(int $count): string",
        "\x1b[32m4\x1b[0m:        return \x1b[1;31mstr_repeat\x1b[0m('banana', $count);",
    ]


def test_output_to_a_pipe_has_no_colour(cache: Path) -> None:
    assert "\x1b" not in run("grep", "str_repeat")


def at_a_terminal(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    """Run the command as if stdout and stderr were a terminal. capsys holds what it printed."""
    monkeypatch.setattr(sys, "argv", ["what", *args])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm")

    with pytest.raises(SystemExit):
        cli.main()


def test_a_terminal_gets_colour_unless_no_color_or_a_dumb_terminal_says_otherwise(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    at_a_terminal(monkeypatch, "grep", "str_repeat")
    assert "\x1b[35msrc/BananaBender.php\x1b[0m" in capsys.readouterr().out

    at_a_terminal(monkeypatch, "grep", "str_repeat", "--color", "never")
    assert "\x1b" not in capsys.readouterr().out

    monkeypatch.setenv("NO_COLOR", "1")
    at_a_terminal(monkeypatch, "grep", "str_repeat")
    assert "\x1b" not in capsys.readouterr().out

    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(sys, "argv", ["what", "grep", "str_repeat"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "\x1b" not in capsys.readouterr().out


def test_a_terminal_sees_how_far_a_long_read_is_and_the_line_is_cleared_before_the_output(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "PROGRESS_FROM", 1)
    monkeypatch.setenv("NO_COLOR", "1")

    at_a_terminal(monkeypatch, "grep", "str_repeat")
    printed = capsys.readouterr()

    assert printed.err == "\r\x1b[Kwhat: reading files, 0 of 1\r\x1b[K"
    assert printed.out.startswith("1 hit in 1 file")


def test_a_pipe_sees_no_progress(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PROGRESS_FROM", 1)

    assert invoke("grep", "str_repeat").stderr == ""


def test_progress_is_not_shown_for_a_short_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(settings, "PROGRESS_FROM", 10)
    progress = cli.Progress()

    progress.show("files", done=0, total=5)
    progress.clear()

    assert capsys.readouterr().err == ""


def run_with_streams(
    *args: str,
    reader_gone: bool = False,
    stderr_closed: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """
    Run the command in a process of its own. reader_gone puts stdout on a pipe that nothing reads, and
    stderr_closed starts the command with no stderr at all, as `2>&-` does in a shell.
    """
    read_end, write_end = os.pipe()
    os.close(read_end)

    try:
        return subprocess.run(
            [sys.executable, "-m", "what", *args],
            check=False,
            stdout=write_end if reader_gone else subprocess.PIPE,
            stderr=None if stderr_closed else subprocess.PIPE,
            preexec_fn=(lambda: os.close(2)) if stderr_closed else None,
        )
    finally:
        os.close(write_end)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["grep", "str_repeat"], 0),
        (["status"], 0),
        (["--help"], 0),
        (["grep", "no_such_fruit"], cli.NOTHING_FOUND),
        (["grep", "-e", "("], cli.ERROR),
        (["grep", "banana", "--limit", "0"], cli.ERROR),
    ],
)
def test_a_command_keeps_its_exit_code_when_its_reader_has_gone(cache: Path, args: list[str], code: int) -> None:
    done = run_with_streams(*args, reader_gone=True)

    assert done.returncode == code
    assert b"BrokenPipeError" not in done.stderr
    assert b"Traceback" not in done.stderr


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["grep", "str_repeat"], 0),
        (["status"], 0),
        (["grep", "no_such_fruit"], cli.NOTHING_FOUND),
        (["grep", "-e", "("], cli.ERROR),
    ],
)
def test_a_command_keeps_its_exit_code_when_it_starts_with_stderr_closed(
    cache: Path,
    args: list[str],
    code: int,
) -> None:
    done = run_with_streams(*args, stderr_closed=True)

    assert done.returncode == code
    assert (done.stdout == b"") == (code != 0)


# macOS has no /dev/full, which is a file that fails every write to it.
@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
@pytest.mark.parametrize("args", [["show", "banana", "--ref", "no-such-ref"], ["grep", "banana", "--limit", "0"]])
def test_an_error_is_still_an_object_on_stdout_when_stderr_cannot_be_written(cache: Path, args: list[str]) -> None:
    with open("/dev/full", "w") as full:
        done = subprocess.run(
            [sys.executable, "-m", "what", "--json", *args], check=False, stdout=subprocess.PIPE, stderr=full
        )

    assert json.loads(done.stdout)["type"] == "error"


def test_a_command_that_raises_is_an_object_on_stdout_with_json(
    cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **options: object) -> None:
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(cli.search, "grep", fail)
    monkeypatch.setattr(sys, "argv", ["what", "grep", "banana", "--json"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    printed = capsys.readouterr()
    assert stopped.value.code == cli.ERROR
    assert json.loads(printed.out) == {"type": "error", "text": "the command failed: what grep banana --json"}
    assert "RuntimeError: the index is on fire" in printed.err


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
def test_output_that_a_full_disk_loses_is_an_error(cache: Path, repo: Path) -> None:
    # Python holds 8 KB of output before it writes any, so the file is bigger than that.
    (repo / "src/long.py").write_text("".join(f"banana_{number} = {number}\n" for number in range(4000)))

    with open("/dev/full", "w") as full:
        done = subprocess.run(
            [sys.executable, "-m", "what", "src/long.py:1-4000"], check=False, stdout=full, stderr=subprocess.PIPE
        )

    assert done.returncode == cli.ERROR
    assert b"No space left on device" in done.stderr
