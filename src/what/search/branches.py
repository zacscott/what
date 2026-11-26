"""Reads the other branches a search falls back to, and words the notes about them."""

from typing import Final, NamedTuple

from what import git
from what.config import settings
from what.index import Index
from what.models import (
    OnBranch,
    Place,
    age,
)
from what.output import Line

BRANCHES_BUILDING: Final = "(The index of other branches is not up to date, so {count} branches may be missing.)"


class Section(NamedTuple):
    """The output of a search beyond the working tree, and notes for its last lines, such as what isn't indexed yet."""

    lines: list[Line]
    notes: list[str]


class BranchesRead(NamedTuple):
    """The other branches, and a note when some of them aren't indexed yet."""

    branches: list[git.Branch]
    notes: list[str]


def read_branches(index: Index) -> BranchesRead:
    """
    The other branches, after reading the ones that moved. When too many moved for a search to wait on, a process
    in the background reads them, and the note says some branches may be missing.
    """
    branches, pending = index.refresh_branches(settings.BRANCHES_READ_INLINE)
    if not pending:
        return BranchesRead(branches, [])

    index.build_in_background()
    return BranchesRead(branches, [BRANCHES_BUILDING.format(count=pending)])


def branches_heading(places: list[Place], *, found_here: bool) -> str:
    """The line before the matches on other branches, such as "not in this checkout; found on 2 other branches:"."""
    count = len({place.origin.name for place in places if isinstance(place.origin, OnBranch)})
    branches = "1 other branch" if count == 1 else f"{count} other branches"
    return f"also on {branches}:" if found_here else f"not in this checkout; found on {branches}:"


def fetch_note(index: Index, branches: list[git.Branch]) -> list[str]:
    """
    When remote-tracking branches were searched, a line saying when they were last fetched, so a missing match isn't
    read as missing code.
    """
    if not any(branch.remote for branch in branches):
        return []

    fetched = git.last_fetch(index.root)
    if fetched is None:
        return ["remotes not fetched since the clone"]
    return [f"remotes fetched {age(int(fetched))}"]
