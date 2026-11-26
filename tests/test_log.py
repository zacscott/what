"""Writes each command, and each failure, to the log file, and limits how often a failed build is started again."""

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import tree_sitter_language_pack as grammars

from what import cli, log, search, symbols
from what.config import settings
from what.index import Index


@pytest.fixture
def command(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the command at the test repository, with an index folder of its own and no background build."""
    monkeypatch.setenv("WHAT_ROOT", str(repo))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    def start_nothing(index: Index) -> None:
        pass

    monkeypatch.setattr(Index, "build_in_background", start_nothing)


def run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["what", *args])
    with pytest.raises(SystemExit):
        cli.main()


def record_builds(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record each background build a test starts, in place of starting it. Returns the commands, which fill up."""
    started: list[list[str]] = []

    def record(command: list[str], **options: object) -> None:
        started.append(command)

    monkeypatch.setattr(subprocess, "Popen", record)
    return started


@pytest.mark.usefixtures("command")
def test_a_command_is_logged_with_how_it_went(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_main(monkeypatch, "BananaBender", "--kind", "class")

    lines = log_file.read_text().splitlines()

    assert " INFO " in lines[-1]
    assert "what BananaBender --kind class in " in lines[-1]
    assert ": ok in " in lines[-1]


@pytest.mark.usefixtures("command")
def test_a_command_is_logged_with_the_folder_it_searched(
    repo: Path,
    tmp_path: Path,
    log_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHAT_ROOT")
    monkeypatch.chdir(tmp_path)

    run_main(monkeypatch, "-C", str(repo / "src"), "BananaBender")

    assert f"what -C {repo}/src BananaBender in {repo}: ok in " in log_file.read_text()


def test_a_worker_logs_to_the_file_and_leaves_ctrl_c_to_the_command(log_file: Path) -> None:
    before = signal.getsignal(signal.SIGINT)

    try:
        symbols.start_worker()
        logging.getLogger("what.symbols").warning("a worker wrote this")

        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert "a worker wrote this" in log_file.read_text()
    finally:
        signal.signal(signal.SIGINT, before)


@pytest.mark.usefixtures("command")
def test_a_search_that_finds_nothing_is_logged_as_that(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_main(monkeypatch, "grep", "no_such_fruit")

    assert "what grep no_such_fruit in " in log_file.read_text()
    assert ": nothing found in " in log_file.read_text()


@pytest.mark.usefixtures("command")
def test_a_command_that_exits_with_an_error_is_logged(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_main(monkeypatch, "show", "bandana", "--ref", "no-such-ref")

    logged = log_file.read_text()

    assert " ERROR " in logged
    assert "'no-such-ref' is not a branch, tag or commit" in logged
    assert "what show bandana --ref no-such-ref in " in logged
    assert ": exit 2 in " in logged


@pytest.mark.usefixtures("command")
def test_a_command_that_raises_is_logged_and_printed_to_stderr_with_its_traceback(
    log_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **options: object) -> str:
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(search, "lookup", fail)
    monkeypatch.setattr(sys, "argv", ["what", "BananaBender"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    logged = log_file.read_text()
    printed = capsys.readouterr()

    assert stopped.value.code == cli.ERROR
    assert printed.out == ""
    assert printed.err.startswith("what: the command failed: what BananaBender\nTraceback")
    assert printed.err.count("RuntimeError: the index is on fire") == 1
    assert " ERROR " in logged
    assert "RuntimeError: the index is on fire" in logged
    assert "what BananaBender in " in logged.splitlines()[-1]
    assert ": failed in " in logged.splitlines()[-1]


@pytest.mark.usefixtures("command")
def test_a_command_runs_when_the_log_file_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "blocked").write_text("a file where the log folder should be")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "blocked"))

    run_main(monkeypatch, "BananaBender")

    assert "src/BananaBender.php:2:class BananaBender extends Lady" in capsys.readouterr().out


def test_a_log_file_that_is_too_big_is_moved_aside(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LOG_MAX_BYTES", 10)
    log_file.parent.mkdir(parents=True)
    log_file.write_text("an old line that is longer than ten bytes\n")

    log.start()

    assert log_file.with_name("what.log.1").read_text() == "an old line that is longer than ten bytes\n"
    assert log_file.read_text() == ""


def test_a_build_that_did_not_finish_is_not_started_again_at_once(
    index: Index,
    log_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = record_builds(monkeypatch)
    log.start()

    index.build_in_background()
    index.build_in_background()

    assert len(started) == 1
    assert "a background build started or failed not long ago" in log_file.read_text()

    with index.connect() as session:
        session.set_meta("build_wait_until", str(time.time() - 1))
    index.build_in_background()

    assert len(started) == 2


def test_a_build_that_finished_lets_the_next_one_start_at_once(index: Index, monkeypatch: pytest.MonkeyPatch) -> None:
    started = record_builds(monkeypatch)

    index.build_in_background()
    index.build_finished()
    index.build_in_background()

    assert len(started) == 2


def test_a_refresh_that_reads_a_file_again_is_logged(index: Index, repo: Path, log_file: Path) -> None:
    log.start()
    (repo / "src/peel.py").write_text("def peel_banana():\n    pass\n")

    search.find(index, "peel_banana")

    assert f"{repo}: read 1 unstaged files again" in log_file.read_text()


def test_a_grammar_that_cannot_be_downloaded_is_logged_once(log_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(text: str, config: object) -> object:
        raise grammars.DownloadError("no network")

    monkeypatch.setattr(grammars, "process", fail)
    monkeypatch.setattr(symbols, "_unavailable", set[str]())
    log.start()

    assert symbols.extract("a.py", "def a(): pass\n") == []
    assert symbols.extract("b.py", "def b(): pass\n") == []

    logged = log_file.read_text()

    assert logged.count("no grammar for python, so its files have no symbols: no network") == 1
    assert " WARNING " in logged


@pytest.mark.usefixtures("command")
def test_a_build_that_fails_is_not_started_again_at_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(index: Index) -> None:
        raise RuntimeError("git went away")

    monkeypatch.setattr(Index, "refresh_history", fail)
    monkeypatch.setattr(sys, "argv", ["what", "index"])
    with pytest.raises(SystemExit):
        cli.main()

    monkeypatch.undo()
    index = Index(repo, cache_dir=tmp_path / "xdg" / "what")
    started = record_builds(monkeypatch)
    index.build_in_background()

    assert started == []


@pytest.mark.usefixtures("command")
def test_a_command_runs_from_a_folder_that_was_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    run_main(monkeypatch, "BananaBender")

    assert "src/BananaBender.php:2:class BananaBender extends Lady" in capsys.readouterr().out


def test_an_error_any_module_logs_is_printed_to_stderr_and_a_warning_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log.show_errors()

    logging.getLogger("what.index").warning("the shelf is nearly full")
    logging.getLogger("what.index").error("the shelf fell over")

    assert capsys.readouterr().err == "what: the shelf fell over\n"
