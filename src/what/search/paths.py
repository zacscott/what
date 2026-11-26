"""Finds the file a path names, reads it, and tells which paths are inside installed dependencies."""

import re
from collections.abc import Callable
from typing import Final, assert_never

from what import dependencies, git
from what.database import IndexSession
from what.index import Index, as_text
from what.models import AtRef, Deleted, InPackage, Live, OnBranch, Place
from what.output import Line, LineKind, Result, note, nothing

# A file and a line or a range of lines, such as "src/a.py:45" or "src/a.py:45-80".
LINE_RANGE: Final = re.compile(r"(.+?):(\d+)(?:-(\d+))?")


# A deleted file as output names it, such as "a1b2c3d4^:legacy/split.py".
DELETED_REF: Final = re.compile(r"([0-9a-f]{4,40})\^:(.+)")


def dependency_paths(index: Index) -> Callable[[str], bool]:
    """
    A test for whether a path in the working tree is inside a folder of installed dependencies, such as vendor/.
    Searches leave those out, even when they are committed, so dependency code is never shown as the repository's.
    """
    folders = tuple(dependencies.dependency_folders(index))
    return lambda path: path.startswith(folders) or "/node_modules/" in path


def resolve_path(session: IndexSession, path: str) -> list[Place]:
    """The file a path names: an exact match if there is one, or else every file whose path ends with it."""
    deleted = DELETED_REF.fullmatch(path)
    if deleted:
        commit, path = deleted.groups()
        return [
            place
            for place in resolve_path(session, path)
            if isinstance(place.origin, Deleted) and place.origin.commit.startswith(commit)
        ]

    path = path.removeprefix("./")
    exact = session.places_at_path(path)
    if exact:
        return exact[:1]
    return session.places_ending_with(path)


def path_choices(path: str, places: list[Place]) -> Result:
    """What to print when a path names no file, or more than one. Neither is an answer, so found is False."""
    if not places:
        return nothing(f"No file matches {path!r}. Use find to search for it.")

    choices = [Line(LineKind.FILE, place=place) for place in places[:20]]
    return Result([note(f"{path!r} matches several files:"), *choices], found=False)


def read_place(index: Index, place: Place) -> str | None:
    """The text of a file, from disk or from git. None when it is binary, too big or gone."""
    match place.origin:
        case Live():
            return index.read_live(place.path)
        case InPackage(folder=folder):
            return index.read_text(folder / place.path)
        case Deleted() | AtRef() | OnBranch():
            content = next(iter(git.read_blobs(index.root, [place.oid])))[1]
            return as_text(content) if content is not None else None
        case _:
            assert_never(place.origin)
