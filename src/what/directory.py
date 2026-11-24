"""
Lists the files of a directory that is not a git repository, in place of git ls-files. The index runs in
directory mode when there is no git repository.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from what.config import settings

if TYPE_CHECKING:
    import pathspec


class TooManyFiles(Exception):
    """The directory has more files than settings.MAX_FILES."""


def files(root: Path) -> list[str]:
    """
    Every file under root as a path relative to it, skipping what .gitignore files ignore and every
    directory whose name starts with a dot. For example ["README.md", "src/app.py"].
    """
    found: list[str] = []
    top = str(root)
    # A .gitignore applies to the directory it is in and everything below it, so each directory keeps the rules
    # of the directories above it.
    inherited: dict[str, list[tuple[str, pathspec.GitIgnoreSpec]]] = {"": []}

    for directory, subdirectories, names in os.walk(top):
        relative = os.path.relpath(directory, top)
        prefix = "" if relative == "." else relative + "/"
        rules = inherited.pop(prefix)

        if ".gitignore" in names:
            own = _read_gitignore(os.path.join(directory, ".gitignore"))
            if own is not None:
                rules = [*rules, (prefix, own)]

        subdirectories[:] = [
            name for name in subdirectories if not name.startswith(".") and not _ignored(rules, prefix + name + "/")
        ]
        for name in subdirectories:
            inherited[prefix + name + "/"] = rules

        found.extend(prefix + name for name in names if not _ignored(rules, prefix + name))

        if len(found) > settings.MAX_FILES:
            raise TooManyFiles(
                f"{root} has more than {settings.MAX_FILES:,} files and is not a git repository. "
                "Run what in a project directory or a git repository."
            )

    return found


def _ignored(rules: list[tuple[str, pathspec.GitIgnoreSpec]], path: str) -> bool:
    """
    True when a rule from any .gitignore above the path ignores it. A rule matches the path from the directory
    its .gitignore is in.
    """
    return any(spec.match_file(path[len(prefix) :]) for prefix, spec in rules)


def _read_gitignore(path: str) -> pathspec.GitIgnoreSpec | None:
    """
    The rules in one .gitignore, or None when the directory has none. re2 matches the rules about twice as fast
    as the default backend.
    """
    # Only directory mode needs pathspec, so it is imported here. Importing it costs about 10ms, which every search
    # in git mode would pay.
    import pathspec

    try:
        with open(path, errors="replace") as file:
            return pathspec.GitIgnoreSpec.from_lines(file.read().splitlines(), backend="re2")
    except OSError:
        return None
