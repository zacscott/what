"""The command line. A coding harness runs these commands through its shell."""

import os
import subprocess
import sys
from pathlib import Path

import click

from what import directory, git, search
from what.index import Index

DESCRIPTION = """\
An index of this git repository: every symbol, every line of text, and every
file that history deleted. It always matches the working tree, uncommitted
edits included. Output is one match per line, as "path:line:text" like grep.

Give a name, a file or a line range, and `what` picks the search. A word that
is also a command, like "index" or "find", needs `what find index`."""

EXAMPLES = """\
\b
examples:
  what BananaBender                where a class, function or file is defined
  what Banana.bend                 one method of one class
  what banana --kind class --path src/
  what src/BananaBender.php        what a file defines, with line ranges
  what src/BananaBender.php:45-80  those lines
  what show Banana.bend            the source of one symbol
  what grep "str_repeat("          text, each hit under the function it sits in
  what grep "bend\\w+" --regex --deleted
  what show Banana.bend --ref origin/dev   the method as it is on another branch
"""

PATH_HELP = "only files whose path contains this, or matches this glob"
REF_HELP = "search the files at this branch, tag or commit in place of the working tree"
KIND_HELP = "only this kind: class, function, method, interface, enum..."


class _LookupWhenNoCommand(click.Group):
    """
    Runs the lookup command when the first argument is not one of the other commands or an option of what
    itself. That is what lets `what Banana` work without a command name.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # lookup is hidden, so a symbol called lookup is searched for like any other name.
        commands = {name for name, command in self.commands.items() if not command.hidden}
        own_options = {option for parameter in self.get_params(ctx) for option in parameter.opts}

        if args and args[0] not in commands and args[0] not in own_options:
            args = ["lookup", *args]

        return super().parse_args(ctx, args)


@click.group(
    cls=_LookupWhenNoCommand,
    help=DESCRIPTION,
    epilog=EXAMPLES,
    invoke_without_command=True,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--index", "build_index", is_flag=True, help="build or update the whole index now, deleted files included"
)
@click.option("--prompt", is_flag=True, help="print the instructions that tell a coding harness to use this command")
def app(build_index: bool, prompt: bool) -> None:
    if prompt:
        # prompt.md goes in a harness's instructions file, such as AGENTS.md.
        print((Path(__file__).with_name("config") / "prompt.md").read_text(), end="")
    elif build_index:
        index = _open_index()
        index.refresh()
        index.refresh_history()


def main() -> None:
    """Run the command line. The installed `what` and `python -m what` both start here."""
    try:
        app(prog_name="what")
    except (directory.TooManyFiles, git.UnknownRef) as error:
        sys.exit(f"what: {error}")


@app.command(hidden=True)
@click.argument("query", nargs=-1, required=True)
@click.option("--kind", help=KIND_HELP)
@click.option("--path", help=PATH_HELP)
@click.option("--limit", default=30, show_default=True)
@click.option("--ref", help=REF_HELP)
def lookup(
    query: tuple[str, ...],
    kind: str | None,
    path: str | None,
    limit: int,
    ref: str | None,
) -> None:
    """Find a name, list what a file defines or print a line range. what runs this when no command is given."""
    print(search.lookup(_searchable_index(), " ".join(query), kind, path, limit, ref))


@app.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--kind", help=KIND_HELP)
@click.option("--path", help=PATH_HELP)
@click.option("--limit", default=30, show_default=True)
@click.option("--ref", help=REF_HELP)
def find(
    query: tuple[str, ...],
    kind: str | None,
    path: str | None,
    limit: int,
    ref: str | None,
) -> None:
    """Find where a class, function, method, type or file is defined, by name or part of one."""
    print(search.find(_searchable_index(), " ".join(query), kind, path, limit, ref))


@app.command()
@click.argument("pattern", nargs=-1, required=True)
@click.option("--regex", is_flag=True, help="treat the pattern as a Python regex")
@click.option("--path", help=PATH_HELP)
@click.option("--deleted", is_flag=True, help="also search files deleted in git history")
@click.option("--commits", is_flag=True, help="also list commits that added or removed the text; slow")
@click.option("--limit", default=40, show_default=True)
def grep(
    pattern: tuple[str, ...],
    regex: bool,
    path: str | None,
    deleted: bool,
    commits: bool,
    limit: int,
) -> None:
    """Search file contents. Each hit is listed under the symbol it sits in. Case is ignored unless the pattern has a capital."""
    index = _searchable_index()
    print(search.grep(index, " ".join(pattern), regex, path, limit, deleted=deleted, commits=commits))


@app.command()
@click.argument("path")
@click.option("--ref", help=REF_HELP)
def outline(path: str, ref: str | None) -> None:
    """List every symbol in one file with its line range. PATH can be the end of a path, and deleted files work too."""
    print(search.outline(_searchable_index(), path, ref))


@app.command()
@click.argument("target")
@click.option("--path", help="picks the file when several define the symbol")
@click.option("--ref", help=REF_HELP)
def show(target: str, path: str | None, ref: str | None) -> None:
    """Print the source of one symbol ("Banana.bend"), a line range ("src/a.py:45-80") or a file."""
    print(search.show(_searchable_index(), target, path, ref))


def _open_index() -> Index:
    root = Path(os.environ.get("WHAT_ROOT", os.getcwd()))
    if not root.is_dir():
        sys.exit(f"what: {root} is not a directory")

    return Index(root)


def _searchable_index() -> Index:
    """The index, with the build of deleted files started when it is out of date."""
    index = _open_index()
    _start_history_build(index)
    return index


def _start_history_build(index: Index) -> None:
    """
    Start indexing the deleted files in a process that outlives this one.
    Reading every deleted file can take a minute, so a search never waits for it.
    """
    if index.history_is_current() or index.history_is_building():
        return

    subprocess.Popen(
        [sys.executable, "-m", "what", "--index"],
        cwd=index.root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # A new session keeps the build alive when the harness kills the shell that ran the search.
        start_new_session=True,
    )
