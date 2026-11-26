"""
The records the index and the searches pass around: where a file is, a symbol that matched and what a search
asks for.
"""

import enum
import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, assert_never

from what.symbols import Kind


class Scope(enum.Enum):
    """Which files a search reads."""

    # The files in the working tree, and the files that HEAD's history deleted.
    WORKING_TREE = enum.auto()

    # The files on other branches whose content differs from the working tree's file at the same path.
    BRANCHES = enum.auto()

    # The files at a git ref whose content the index holds, as IndexSession.load_ref_files listed them.
    REF = enum.auto()


@dataclass(frozen=True, slots=True)
class Live:
    """A file in the working tree, read from disk."""


@dataclass(frozen=True, slots=True)
class Deleted:
    """A file that HEAD's history deleted, read from git as it was in the commit before commit."""

    commit: str


@dataclass(frozen=True, slots=True)
class AtRef:
    """A file read from git at a ref, which holds the ref as the user gave it, such as "origin/dev"."""

    ref: str


@dataclass(frozen=True, slots=True)
class OnBranch:
    """A file on another branch, read from git. date is the time of the branch's last commit, in seconds snice 1970."""

    name: str
    date: int


@dataclass(frozen=True, slots=True)
class InPackage:
    """
    A file in an installed dependency, read from disk. label names the package, such as
    "vendor: robmorgan/phinx@0.16.1", and the file's path is relative to folder.
    """

    label: str
    folder: Path


# Where a file's content comes from.
type Origin = Live | Deleted | AtRef | OnBranch | InPackage


@dataclass(frozen=True, slots=True)
class Place:
    """A file a search found: its path, the git blob id and index row of its content, and where it cones from."""

    path: str
    oid: str
    blob_row: int
    origin: Origin = Live()

    @property
    def ref(self) -> str:
        """
        How output names the file. A deleted file is named the way git names a file at a commit, in the commit
        before the one that deleted it, such as "a1b2c3d4^:legacy/split.py". A file at a ref is named
        "origin/dev:src/a.py", and a file on a branch "origin/dev (3 days ago) src/a.py".
        """
        match self.origin:
            case Live():
                return self.path
            case Deleted(commit=commit):
                return f"{commit[:8]}^:{self.path}"
            case AtRef(ref=ref):
                return f"{ref}:{self.path}"
            case OnBranch(name=name, date=date):
                return f"{name} ({age(date)}) {self.path}"
            case InPackage(label=label):
                return f"{label} {self.path}"
            case _:
                assert_never(self.origin)

    @property
    def is_live(self) -> bool:
        """True when the file is in the working tree."""
        return isinstance(self.origin, Live)

    @property
    def is_deleted(self) -> bool:
        """True when HEAD's history deleted the file."""
        return isinstance(self.origin, Deleted)


@dataclass(frozen=True, slots=True)
class NameQuery:
    """
    A search fpr symbols by name. name is the name, without its class. words are the parts of the query, which a
    name must each contain to match loosely. parent, kind and path narrow the search when they are set. For example
    "Banana.bend" gives NameQuery("bend", ("Banana.bend",), "Banana", None, None).
    """

    name: str
    words: tuple[str, ...]
    parent: str | None
    kind: Kind | None
    path: str | None


@dataclass(frozen=True, slots=True)
class SymbolHit:
    """
    One symbol a search found, and the file it is in. The searches take the same
    filters. parent and kind must match exactly, and path works as path_matches does.
    """

    place: Place
    name: str
    kind: Kind
    parent: str | None
    line: int
    end_line: int
    signature: str


class Counts(NamedTuple):
    """How many of each thing the index holds."""

    files: int
    symbols: int
    deleted: int
    branches: int


class KnownFile(NamedTuple):
    """
    What the index holds for a path in the working tree: its blob id, and the stat it had when read. stat is None for
    a file git tracks unchanged, whose blob id says when it changes.
    """

    oid: str
    stat: str | None


class BranchListing(NamedTuple):
    """
    The commits a branch's tip and HEAD pointed at when the branch's files were listed. head is empty for a branch
    that isn't compared with HEAD.
    """

    tip: str
    head: str


class RefFile(NamedTuple):
    """A file at a git ref: its path, the git blob id of its content, and how that content is parsed."""

    path: str
    oid: str
    parsed_as: str


@dataclass(frozen=True, slots=True)
class PackageSymbol:
    """One definition in an installed dependency. path is relative to the package's folder."""

    package: int
    path: str
    name: str
    kind: Kind
    parent: str | None
    line: int
    end_line: int
    signature: str


def age(timestamp: int) -> str:
    """How long ago a time was, in its largest whole unit, such as "3 days ago" or "5 months ago"."""
    seconds = max(0, int(time.time()) - timestamp)

    for unit, length in [("year", 365 * 86400), ("month", 30 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60)]:
        if seconds >= length:
            count = seconds // length
            return f"{count} {unit} ago" if count == 1 else f"{count} {unit}s ago"

    return "just now"


def path_matches(path: str, path_filter: str | None) -> bool:
    """True when path contains the filter, or matches it as a glob when it has a wildcard."""
    if not path_filter:
        return True
    if any(wildcard in path_filter for wildcard in "*?["):
        return fnmatch.fnmatch(path, path_filter) or fnmatch.fnmatch(path, "*/" + path_filter)
    return path_filter.lower() in path.lower()
