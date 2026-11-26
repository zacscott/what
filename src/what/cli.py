"""The command line. A coding harness runs these commands through its shell, and so does a person at a terminal."""

import io
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NoReturn, Protocol, TypedDict, Unpack, override

import click

from what import database, dependencies, directory, git, log, output, search, streams
from what.config import settings
from what.dependencies import DependencyReach
from what.index import Index, Reading
from what.output import Line, LineKind, Result, plural
from what.search import Case
from what.symbols import Kind

logger = logging.getLogger(__name__)

DESCRIPTION: Final = """\
An index of this git repository: every symbol, every line of text, and every
file that history deleted. It always matches the working tree, uncommitted
edits included.

Give a name, a file or a line range, and `what` picks the search. A word that
is also a command, like "index" or "status", needs `what find index`.

A search prints one match per line, as "path:line:text" like grep. It exits
with 0 when it found something, 1 when it found nothing and 2 for an error."""

EXAMPLES: Final = """\
\b
examples:
  what BananaBender                where a class, function or file is defined
  what Banana.bend                 one method of one class
  what banana --kind class --path src/
  what src/BananaBender.php        what a file defines, with line ranges
  what src/BananaBender.php:45-80  those lines
  what show Banana.bend            the source of one symbol
  what grep "str_repeat("          text, each hit under the function it sits in
  what grep -e "bend\\w+" --deleted
  what grep "punctuate" --branches  also search unmerged branches
  what show Phinx\\Db\\Table.addIndex   falls back to installed dependencies
  what callers Banana.bend           the lines that call one method
  what show Banana.bend --ref origin/dev   the method as it is on another branch
  what -C ../other-repo status       the index of another folder
"""

PATH_HELP: Final = "only files whose path contains this, or matches this glob"
BRANCHES_HELP: Final = "also search other branches; without it they are searched only when nothing else matches"
DEPS_HELP: Final = "also search the direct dependencies installed from the lockfiles"
ALL_DEPS_HELP: Final = "also search every installed dependency, the dependencies of dependencies included"
REF_HELP: Final = "search the files at this branch, tag or commit in place of the working tree"
KIND_HELP: Final = "only symbols of this kind"
LIMIT_HELP: Final = "the most matches shown from each place searched"
# --kind accepts only the kinds the index holds, so a typo is an error and not an empty result.
KIND_TYPE: Final = click.Choice([kind.value for kind in Kind], case_sensitive=False)

# The exit codes, which are the ones grep uses.
NOTHING_FOUND: Final = 1
ERROR: Final = 2
# What a shell reports for a command that Ctrl-C stopped.
INTERRUPTED: Final = 130

# The options that were commands' names before 0.2, and the commands they are now.
OLD_SPELLINGS: Final = {"--index": "index", "--prompt": "prompt"}


class Color(StrEnum):
    """When a search prints for a terminal: in colour, with the matches of one file under its path."""

    # Only when stdout is a terminal.
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class Progress:
    """
    Shows on stderr how far a long read of the index is, on one line that is written over. The line is cleared
    before anything else is printed. A run shows progress only when stderr is a terminal.
    """

    def __init__(self) -> None:
        self._shown = False

    def show(
        self,
        reading: Reading,
        *,
        done: int,
        total: int,
    ) -> None:
        if total < settings.PROGRESS_FROM:
            return

        # "\r" goes back to the start of the line and "\x1b[K" clears it.
        streams.err(f"\r\x1b[Kwhat: reading {reading}, {done:,} of {total:,}", end="")
        self._shown = True

    def clear(self) -> None:
        if self._shown:
            streams.err("\r\x1b[K", end="")
            self._shown = False


@dataclass(slots=True)
class _Run:
    """
    The options of one run that the code for opening the index and printing reads, and its progress line. root is
    the folder whose index the run opened, for the log.
    """

    folder: Path | None = None
    root: Path | None = None
    as_json: bool = False
    color: Color = Color.AUTO
    progress: Progress = field(default_factory=Progress)


def _run(ctx: click.Context | None = None) -> _Run:
    """This run's options. They are kept on the context of `what` itself, which every command's context leads to."""
    root = (ctx or click.get_current_context()).find_root()
    if root.obj is None:
        root.obj = _Run()
        # click closes the context when the command ends or fails, before an error is printed.
        root.call_on_close(root.obj.progress.clear)

    return root.obj


class _LookupWhenNoCommand(click.Group):
    """
    Runs the lookup command when the first argument after what's own options is not one of the other commands.
    That is what lets `what Banana` work without a command name.
    """

    @override
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # lookup is hidden, so a symbol called lookup is searched for like any other name.
        commands = {name for name, command in self.commands.items() if not command.hidden}
        position = _after_own_options(args, self.get_params(ctx))

        if position < len(args) and args[position] in OLD_SPELLINGS:
            args = [*args[:position], OLD_SPELLINGS[args[position]], *args[position + 1 :]]
        elif position < len(args) and args[position] not in commands:
            args = [*args[:position], "lookup", *args[position:]]

        return super().parse_args(ctx, args)

    @override
    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        """Read the options of what itself. --help and --version print from here."""
        try:
            return super().make_context(info_name, args, parent, **extra)
        except BrokenPipeError as error:
            raise streams.broken_pipe() from error

    @override
    def invoke(self, ctx: click.Context) -> object:
        """Run the command, and turn the errors a user can cause into a one-line message."""
        try:
            return super().invoke(ctx)
        except (directory.TooManyFiles, git.UnknownRef, search.InvalidPattern) as error:
            _fail(str(error))
        except BrokenPipeError as error:
            raise streams.broken_pipe() from error


def _after_own_options(args: list[str], parameters: list[click.Parameter]) -> int:
    """How many of the first arguments are options of what itself, such as "-C ../other" or "--version"."""
    options = [parameter for parameter in parameters if isinstance(parameter, click.Option)]
    flags = {name for option in options if option.is_flag for name in option.opts}
    with_value = {name for option in options if not option.is_flag for name in option.opts}
    position = 0

    while position < len(args):
        argument = args[position]
        if argument in with_value:
            position += 2
        elif argument in flags or any(_holds_its_value(argument, name) for name in with_value):
            position += 1
        else:
            break

    return position


def _holds_its_value(argument: str, option: str) -> bool:
    """True when an argument is an option and its value in one word, such as "--color=never" or "-C../other"."""
    if option.startswith("--"):
        return argument.startswith(option + "=")

    return argument.startswith(option) and len(argument) > len(option)


class _NoCommandContext(click.Context):
    """Names the lookup command as plain `what` in a usage error, since nobody types its hidden name."""

    @property
    @override
    def command_path(self) -> str:
        return self.parent.command_path if self.parent else ""


class _Lookup(click.Command):
    context_class = _NoCommandContext


def _set_folder(
    ctx: click.Context,
    parameter: click.Parameter,
    value: Path | None,
) -> None:
    if value is not None:
        _run(ctx).folder = value


def _set_json(
    ctx: click.Context,
    parameter: click.Parameter,
    value: bool,
) -> None:
    if value:
        _run(ctx).as_json = True


def _set_color(
    ctx: click.Context,
    parameter: click.Parameter,
    value: str | None,
) -> None:
    if value is not None:
        _run(ctx).color = Color(value)


def _output_options[Command: Callable[..., object]](command: Command) -> Command:
    """
    Add the options that say how a search prints what it found. `what` itself has them too, so they work before the
    command's name as well as after it. Each one is kept on the run, so a command needs no parameter for it.
    """
    as_json = click.option(
        "--json",
        is_flag=True,
        expose_value=False,
        callback=_set_json,
        help="print one JSON object for each line of output",
    )
    color = click.option(
        "--color",
        type=click.Choice([color.value for color in Color]),
        expose_value=False,
        callback=_set_color,
        help="colour, and matches grouped under their file; auto, the default, does this only at a terminal",
    )
    return as_json(color(command))


@click.group(
    cls=_LookupWhenNoCommand,
    help=DESCRIPTION,
    epilog=EXAMPLES,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "-C",
    "folder",
    metavar="FOLDER",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    expose_value=False,
    callback=_set_folder,
    help="run as if started in this folder, like git -C",
)
@_output_options
@click.version_option(None, "-V", "--version", package_name="what", prog_name="what", message="%(prog)s %(version)s")
def app() -> None:
    pass


def main() -> None:
    """
    Run the command line and log how it went. The installed `what` and `python -m what` both start here, so the
    background build logs its failures too.
    """
    log.start()
    command = " ".join(sys.argv[1:])
    run = _Run()
    started = time.perf_counter()
    outcome = "ok"

    try:
        _run_command(run)
    except SystemExit as stop:
        outcome = _outcome(stop.code)
        raise
    except Exception:
        outcome = "failed"
        run.progress.clear()
        # The failure may have come before click read --json.
        if run.as_json or "--json" in _options_typed():
            _print_error(f"the command failed: what {command}")
        # The log prints this and its traceback to stderr. Python would exit with 1, which means nothing was found.
        logger.exception("the command failed: what %s", command)
        sys.exit(ERROR)
    finally:
        run.progress.clear()
        elapsed = round(1000 * (time.perf_counter() - started))
        # A command that opened no index, such as prompt, is logged with the folder it ran in.
        folder = run.root or _working_folder()
        logger.info("what %s in %s: %s in %dms", command, folder, outcome, elapsed)


def _outcome(code: str | int | None) -> str:
    """How a command ended, for the log. click ends every command with SystemExit, and its code says how."""
    if not code:
        return "ok"
    if code == NOTHING_FOUND:
        return "nothing found"

    return f"exit {code}"


def _working_folder() -> str:
    """The folder the command runs in, for the log. A shell can sit in a folder that has since been deleted."""
    try:
        return os.getcwd()
    except OSError:
        return "a deleted folder"


def _run_command(run: _Run) -> None:
    """
    Run the command with the given run to keep its options on, so the caller can read them afterwards. click is
    told not to end the process itself, so a mistake in the options can be printed as JSON when --json asks for it.
    """
    try:
        code = app.main(prog_name="what", obj=run, standalone_mode=False)
    except click.ClickException as error:
        # The mistake may have stopped click before it read --json.
        _show_mistake(error, as_json=run.as_json or "--json" in _options_typed())
        code = error.exit_code
    except click.Abort:
        streams.err("Aborted!")
        code = INTERRUPTED
    except streams.ReaderGone:
        streams.stop_writing()
        code = 0
    except BrokenPipeError as error:
        # click prints a blank line when Ctrl-C stops a command, and that fails when the reader has gone.
        if not isinstance(error.__context__, KeyboardInterrupt) or not streams.reader_has_gone():
            raise
        streams.stop_writing()
        code = INTERRUPTED
    finally:
        streams.flush()

    # click returns the code a command exited with, and whatever else a command returned when it just ended.
    sys.exit(code if isinstance(code, int) else 0)


def _options_typed() -> list[str]:
    """The arguments before a "--". The ones after it are text to search for, even when they look like options."""
    arguments = sys.argv[1:]
    return arguments[: arguments.index("--")] if "--" in arguments else arguments


def _show_mistake(error: click.ClickException, *, as_json: bool) -> None:
    """
    Report a mistake in the options. The log prints it to stderr as click words it, with the usage line. With
    --json it is an object on stdout as well.
    """
    if as_json:
        _print_error(error.format_message())

    # click prints to a file it is given, so this reads what it would print.
    shown = io.StringIO()
    error.show(file=shown)
    logger.error("%s", error.format_message(), extra={log.SHOWN_AS: shown.getvalue().rstrip("\n")})


def _print_error(message: str) -> None:
    """
    Print an error as the object --json gives it, on stdout. This comes before the error is logged, which prints
    it to stderr, so that a stderr nothing reads can't stop the object.
    """
    try:
        streams.out(output.as_json([Line(LineKind.ERROR, text=message)]))
    except OSError:
        # stdout can't take the object, as on a full disk. The error still goes to stderr and the log.
        pass


def _fail(message: str) -> NoReturn:
    """
    End the run with an error the user caused, such as a ref that doesn't exist. The log prints the message to
    stderr. With --json it is an object on stdout as well. The command exits with 2.
    """
    run = _run()
    run.progress.clear()

    if run.as_json:
        _print_error(message)

    logger.error("%s", message)
    click.get_current_context().exit(ERROR)


class _NameOptions(TypedDict):
    """The options that `what <name>` and `find` share, as click passes them to a command."""

    kind: str | None
    path: str | None
    limit: int
    ref: str | None
    branches: bool
    deps: bool
    all_deps: bool


class _NameSearch(Protocol):
    """A search by name: search.lookup or search.find."""

    def __call__(
        self,
        index: Index,
        query: str,
        *,
        kind: Kind | None,
        path: str | None,
        limit: int,
        ref: str | None,
        branches: bool,
        deps: DependencyReach,
    ) -> Result: ...


def _name_options[Command: Callable[..., object]](command: Command) -> Command:
    """Add the argument and the options that `what <name>` and `find` share."""
    parameters = [
        click.argument("query", nargs=-1, required=True),
        click.option("--kind", type=KIND_TYPE, help=KIND_HELP),
        click.option("--path", help=PATH_HELP),
        click.option("--limit", type=click.IntRange(min=1), default=30, show_default=True, help=LIMIT_HELP),
        click.option("--ref", help=REF_HELP),
        click.option("--branches", is_flag=True, help=BRANCHES_HELP),
        click.option("--deps", is_flag=True, help=DEPS_HELP),
        click.option("--all-deps", is_flag=True, help=ALL_DEPS_HELP),
        _output_options,
    ]
    # A decorator written last is applied first, and help lists the options in the order they are written.
    for parameter in reversed(parameters):
        command = parameter(command)

    return command


def _search_names(
    search_names: _NameSearch,
    query: tuple[str, ...],
    options: _NameOptions,
) -> None:
    """Run a search by name with a command's options, and print what it found."""
    _print(
        search_names(
            _searchable_index(),
            " ".join(query),
            kind=_kind(options["kind"]),
            path=options["path"],
            limit=options["limit"],
            ref=options["ref"],
            branches=options["branches"],
            deps=_reach(deps=options["deps"], all_deps=options["all_deps"]),
        )
    )


@app.command(cls=_Lookup, hidden=True)
@_name_options
def lookup(query: tuple[str, ...], **options: Unpack[_NameOptions]) -> None:
    """Find a name, list what a file defines or print a line range. what runs this when no command is given."""
    _search_names(search.lookup, query, options)


@app.command()
@_name_options
def find(query: tuple[str, ...], **options: Unpack[_NameOptions]) -> None:
    """
    Find where a class, function, method, type or file is defined, by name or part of one. The names that are
    exactly the query come first.
    """
    _search_names(search.find, query, options)


@app.command()
@click.argument("pattern", nargs=-1, required=True)
@click.option("-e", "-E", "--regex", is_flag=True, help="treat the pattern as a Python regex")
@click.option("-i", "--ignore-case", is_flag=True, help="ignore case, even when the pattern has a capital")
@click.option("-s", "--case-sensitive", is_flag=True, help="match case, even when the pattern has no capital")
@click.option("-w", "--word", is_flag=True, help="match the pattern only as a whole word")
@click.option("-l", "--files", "files_only", is_flag=True, help="list each matching file once, without its lines")
@click.option(
    "-C",
    "--context",
    type=click.IntRange(min=0),
    default=0,
    metavar="LINES",
    help="also show this many lines around each match",
)
@click.option("--path", help=PATH_HELP)
@click.option("--deleted", is_flag=True, help="also search files deleted in git history")
@click.option("--commits", is_flag=True, help="also list commits that added or removed the text; slow")
@click.option("--branches", is_flag=True, help=BRANCHES_HELP)
@click.option("--deps", is_flag=True, help=DEPS_HELP)
@click.option("--all-deps", is_flag=True, help=ALL_DEPS_HELP)
@click.option("--limit", type=click.IntRange(min=1), default=40, show_default=True, help=LIMIT_HELP)
@_output_options
def grep(
    pattern: tuple[str, ...],
    *,
    regex: bool,
    ignore_case: bool,
    case_sensitive: bool,
    word: bool,
    files_only: bool,
    context: int,
    path: str | None,
    deleted: bool,
    commits: bool,
    branches: bool,
    deps: bool,
    all_deps: bool,
    limit: int,
) -> None:
    """
    Search file contents. The first line of the symbol a hit sits in comes before the hit, with "-" in place of
    ":". Case is ignored unless the pattern has a capital.
    """
    index = _searchable_index()
    reach = _reach(deps=deps, all_deps=all_deps)
    _print(
        search.grep(
            index,
            " ".join(pattern),
            regex=regex,
            case=_case(ignore_case=ignore_case, case_sensitive=case_sensitive),
            word=word,
            path=path,
            limit=limit,
            files_only=files_only,
            context=context,
            deleted=deleted,
            commits=commits,
            branches=branches,
            deps=reach,
        )
    )


@app.command()
@click.argument("target")
@click.option("--path", help=PATH_HELP)
@click.option("--limit", type=click.IntRange(min=1), default=40, show_default=True, help="the most calls shown")
@_output_options
def callers(target: str, *, path: str | None, limit: int) -> None:
    """List the lines that call or reference a symbol ("Banana.bend"), each under the function it sits in."""
    _print(search.callers(_searchable_index(), target, path=path, limit=limit))


@app.command()
@click.argument("path")
@click.option("--ref", help=REF_HELP)
@_output_options
def outline(path: str, ref: str | None) -> None:
    """
    List every symbol in one file as "first-last:signature", under the file's path. PATH can be the end of a
    path, and deleted files work too.
    """
    _print(search.outline(_searchable_index(), path, ref=ref))


@app.command()
@click.argument("target")
@click.option("--path", help="picks the file when several define the symbol")
@click.option("--ref", help=REF_HELP)
@click.option("--deps", is_flag=True, help=DEPS_HELP)
@click.option("--all-deps", is_flag=True, help=ALL_DEPS_HELP)
@_output_options
def show(
    target: str,
    *,
    path: str | None,
    ref: str | None,
    deps: bool,
    all_deps: bool,
) -> None:
    """
    Print the source of one symbol ("Banana.bend"), a line range ("src/a.py:45-80") or a file. Each line is
    "line:text", under a "path:first-last" heading.
    """
    _print(search.show(_searchable_index(), target, path=path, ref=ref, deps=_reach(deps=deps, all_deps=all_deps)))


@app.command("index")
def build() -> None:
    """
    Build or update the whole index now: the working tree, the files that history deleted, the other branches
    and the direct dependencies. A search does this by itself, and reads the slow parts in the background.
    """
    index = _open_index()
    try:
        index.refresh()
        index.refresh_history()
        index.refresh_branches()
        dependencies.package_rows(index, dependencies.installed(index, transitive=False).packages)
    except Exception:
        index.build_failed()
        raise

    index.build_finished()
    counts = index.status().counts
    _run().progress.clear()
    streams.out(
        f"The index holds {plural(counts.files, 'file')}, {plural(counts.deleted, 'deleted file')} "
        f"and {plural(counts.branches, 'other branch', 'other branches')}."
    )


@app.command()
def prompt() -> None:
    """Print the instructions that tell a coding harness to use this command, for a file such as AGENTS.md."""
    streams.out((Path(__file__).with_name("config") / "prompt.md").read_text().removesuffix("\n"))


@app.command()
def status() -> None:
    """Show what the index of this repository holds, whether it is up to date and where its files are."""
    index = _open_index()
    if not index.path.exists():
        # Reading the status of an index creates its file, and status only looks.
        streams.out(f"{index.root} has no index yet. The first search, or `what index`, builds it at {index.path}.")
        return

    state = index.status()
    counts = state.counts

    if state.building:
        building = "running now"
    elif state.build_wait:
        minutes = max(1, round(state.build_wait / 60))
        building = f"started or failed not long ago; a search may start it again in {plural(minutes, 'minute')}"
    else:
        building = "not running"

    rows = [
        ("folder", str(index.root)),
        ("index", f"{index.path} ({state.size / 1_000_000:.1f} MB, schema {database.SCHEMA_VERSION})"),
        ("files", f"{counts.files:,} with {counts.symbols:,} symbols"),
        ("deleted files", f"{counts.deleted:,} ({state.history.value})"),
        ("other branches", f"{counts.branches:,}"),
        ("background build", building),
        ("log", str(log.path())),
    ]
    width = max(len(name) for name, _ in rows)
    lines = [f"{name:<{width}}  {value}" for name, value in rows]
    if not counts.files:
        lines.append("The index is empty. The first search, or `what index`, builds it.")

    streams.out("\n".join(lines))


def _print(result: Result) -> None:
    """
    Print what a search found. A search that found nothing prints to stderr, so a pipe gets no lines, and exits
    with 1. With --json every line goes to stdout as an object, whether anything was found or not.
    """
    run = _run()
    run.progress.clear()

    if run.as_json:
        printed = output.as_json(result.lines)
        if printed:
            streams.out(printed)
    elif not result.found:
        streams.err(result.text)
    elif _for_terminal(run.color):
        streams.out(output.for_terminal(result.lines))
    else:
        streams.out(result.text)

    if not result.found:
        click.get_current_context().exit(NOTHING_FOUND)


def _for_terminal(color: Color) -> bool:
    """Whether to print for a terminal. NO_COLOR is the variable many commands read for "never colour"."""
    if color != Color.AUTO:
        return color == Color.ALWAYS

    # Python sets sys.stdout to None when the command starts with its output closed.
    return (
        bool(sys.stdout) and sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    )


def _case(*, ignore_case: bool, case_sensitive: bool) -> Case:
    """How grep treats capital letters, from its -i and -s options."""
    if ignore_case and case_sensitive:
        raise click.UsageError("-i and -s can't be used together.")
    if ignore_case:
        return Case.IGNORE

    return Case.SENSITIVE if case_sensitive else Case.SMART


def _kind(value: str | None) -> Kind | None:
    """The --kind option as a Kind. click has already checked that it is one."""
    return Kind(value.lower()) if value else None


def _reach(*, deps: bool, all_deps: bool) -> DependencyReach:
    """When a search reads the installed dependencies, from the --deps and --all-deps options."""
    if all_deps:
        return DependencyReach.ALL
    return DependencyReach.DIRECT if deps else DependencyReach.FALLBACK


def _open_index() -> Index:
    """The index of the folder given with -C, or in WHAT_ROOT, or else of the folder the command runs in."""
    run = _run()
    root = Path(run.folder or os.environ.get("WHAT_ROOT") or os.getcwd())
    if not root.is_dir():
        _fail(f"{root} is not a directory")

    # Progress is for a person. A harness or a pipe would get a line of control characters.
    at_a_terminal = bool(sys.stderr) and sys.stderr.isatty()
    index = Index(root, report=run.progress.show) if at_a_terminal else Index(root)
    run.root = index.root
    return index


def _searchable_index() -> Index:
    """The index, with the build of deleted files started when it is out of date."""
    index = _open_index()
    _start_history_build(index)
    return index


def _start_history_build(index: Index) -> None:
    """Start indexing the deleted files in another process when that index is out of date."""
    if not index.history_is_current():
        index.build_in_background()
