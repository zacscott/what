"""Searches for symbols and files by name, or by part of a name."""

import re
from functools import cache
from typing import Final, NamedTuple

from what.config import settings
from what.database import IndexSession
from what.dependencies import DependencyReach
from what.index import Index
from what.models import NameQuery, Place, Scope, SymbolHit, path_matches
from what.output import Line, LineKind, Result, Span, note, nothing, plural
from what.search.at_ref import paths_at, symbol_tiers_at, tree_at
from what.search.branches import Section, branches_heading, fetch_note, read_branches
from what.search.paths import dependency_paths
from what.search.patterns import matcher_for
from what.search.ranking import Qualified, best_first, dependency_symbols, split_qualified, symbols_matching
from what.search.text import Listing, text_search
from what.symbols import Kind

# The words of a name or path, split at case changes, digits and punctuation. For example
# "getHTTPRequest_v2" gives get, HTTP, Request, v and 2.
NAME_WORDS: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


# find prints this line between the names that are exactly the query and the names that only start with it or
# contain it.
PARTIAL_MATCHES: Final = "partial matches:"


def find(
    index: Index,
    query: str,
    *,
    kind: Kind | None = None,
    path: str | None = None,
    limit: int = 30,
    ref: str | None = None,
    branches: bool = False,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> Result:
    """
    Symbols and file paths whose name matches the query. With a ref, the search reads the files at that ref from git
    in place of the working tree. Other branches are searched as well when branches is True, or when the working
    tree has no match. deps says when the installed dependencies are searched.
    """
    if ref is not None:
        return find_at_ref(index, query, kind=kind, path=path, limit=limit, ref=ref)

    index.refresh()
    return find_live(index, query, kind=kind, path=path, limit=limit, branches=branches, deps=deps)


def find_live(
    index: Index,
    query: str,
    *,
    kind: Kind | None,
    path: str | None,
    limit: int,
    branches: bool,
    deps: DependencyReach,
) -> Result:
    words = query.split()
    if not words:
        return nothing("Give a symbol name or part of a file path.")

    parent, name = _split_query(words)
    wanted = NameQuery(name, tuple(words), parent, kind, path)

    in_dependencies = dependency_paths(index)
    with index.connect() as session:
        hits = symbols_matching(session, wanted)
        paths = [] if kind else _find_paths(session, words, path_filter=path)
        # A query with a "/" is a path typed in full, so it is not short for anything.
        abbreviated = not hits and not paths and "/" not in query
        if abbreviated:
            hits, paths = _find_abbreviated(session, wanted)

    hits = [hit for hit in hits if not in_dependencies(hit.place.path)]
    paths = [place for place in paths if not in_dependencies(place.path)]

    here = _found_lines(hits, paths, limit=limit, name=name) if hits or paths else []
    if here and abbreviated:
        # These are guesses, so the reader is told that nothing matched the query as it was typed.
        here.insert(0, note(f"No name or path contains {query!r}. It may be short for:"))

    elsewhere: list[Line] = []
    notes: list[str] = []
    if branches or not here:
        elsewhere, notes = _find_on_branches(index, wanted, limit=limit, found_here=bool(here))

    if deps != DependencyReach.FALLBACK or not here:
        package_hits, package_notes = dependency_symbols(index, wanted, deps=deps)
        elsewhere += _found_lines(best_first(package_hits), [], limit=limit, name=name)
        notes += package_notes

    if here or elsewhere:
        return Result([*here, *elsewhere, *map(note, notes)])

    if kind:
        return nothing(f"No {kind} named {query!r}. Run find without --kind to see other matches.", *notes)

    # A grammar can miss a definition, so the text of the files is searched before saying "nothing".
    text_matches = text_search(
        index, matcher_for(query, regex=False), path=path, deleted=False, listing=Listing(limit=limit)
    ).lines
    if not text_matches:
        return nothing(
            f"Nothing named or containing {query!r} in live or deleted files, other branches or dependencies.", *notes
        )

    return Result([note(f"No symbol or path named {query!r}. Text matches:"), *text_matches, *map(note, notes)])


def _split_query(words: list[str]) -> Qualified:
    """
    The class and the name a query asks for. A query with a "/" is a path, and its dots are not the dot of
    "Class.method", so it has no class.
    """
    query = "".join(words)
    return Qualified(None, query) if "/" in query else split_qualified(query)


def _find_on_branches(
    index: Index,
    wanted: NameQuery,
    *,
    limit: int,
    found_here: bool,
) -> Section:
    """
    The output of find on other branches, empty when no branch has a match that the working tree lacks, and a note
    when some branches aren't indexed yet.
    """
    branches, notes = read_branches(index)
    if not branches:
        return Section([], notes)

    with index.connect() as session:
        hits = symbols_matching(session, wanted, scope=Scope.BRANCHES)
        paths = (
            []
            if wanted.kind
            else _find_paths(session, list(wanted.words), path_filter=wanted.path, scope=Scope.BRANCHES)
        )

    if not hits and not paths:
        return Section([], notes)

    places = [hit.place for hit in hits[:limit]] + paths
    found = [
        note(branches_heading(places, found_here=found_here)),
        *_found_lines(hits, paths, limit=limit, name=wanted.name),
        *map(note, fetch_note(index, branches)),
    ]
    return Section(found, notes)


def _found_lines(
    hits: list[SymbolHit],
    paths: list[Place],
    *,
    limit: int,
    name: str,
) -> list[Line]:
    """
    The output of find, with no more than limit matches: the symbols called exactly name, then the symbols that
    only start with it or contain it, then the files whose path matched. Each group of symbols is listed by file.
    """
    shown = hits[:limit]
    exact = [hit for hit in shown if hit.name.lower() == name.lower()]
    partial = [hit for hit in shown if hit.name.lower() != name.lower()]

    output = _symbol_lines(exact, name=name)
    if exact and partial:
        output.append(note(PARTIAL_MATCHES))
    output.extend(_symbol_lines(partial, name=name))

    listed = {hit.place.path for hit in shown}
    unlisted = [place for place in paths if place.path not in listed]
    room = max(0, min(settings.PATHS_SHOWN, limit - len(shown)))
    output.extend(Line(LineKind.FILE, place=place) for place in unlisted[:room])

    if len(hits) > limit:
        more_files = len({hit.place for hit in hits[limit:]} - {hit.place for hit in shown})
        output.append(
            note(f"+{len(hits) - limit} more symbols in {more_files} other files. Add --path or --kind to narrow.")
        )
    if len(unlisted) > room:
        output.append(note(f"+{plural(len(unlisted) - room, 'more file')} with a matching path."))

    return output


def _symbol_lines(hits: list[SymbolHit], *, name: str) -> list[Line]:
    """One line for each symbol. The symbols of one file are together, in line order, at the place of its best hit."""
    by_place: dict[Place, list[SymbolHit]] = {}
    for hit in hits:
        by_place.setdefault(hit.place, []).append(hit)

    return [
        Line(
            LineKind.MATCH,
            text=hit.signature,
            place=hit.place,
            line=hit.line,
            spans=_name_span(hit.signature, name),
        )
        for group in by_place.values()
        for hit in sorted(group, key=lambda hit: hit.line)
    ]


def _name_span(signature: str, name: str) -> tuple[Span, ...]:
    """Where the name a search asked for is in a signature, for a terminal to highlight."""
    start = signature.lower().find(name.lower())
    return (Span(start, start + len(name)),) if start != -1 else ()


def find_at_ref(
    index: Index,
    query: str,
    *,
    kind: Kind | None,
    path: str | None,
    limit: int,
    ref: str,
) -> Result:
    """find, on the files at a git ref. Only the files that contain the name are parsed."""
    words = query.split()
    if not words:
        return nothing("Give a symbol name or part of a file path.")

    parent, name = _split_query(words)
    commit, tree = tree_at(index, ref)

    # A symbol's name never holds its class, so "Banana.bend" looks for names that contain "bend".
    wanted = NameQuery(name, tuple(words) if len(words) > 1 else (name,), parent, kind, path)
    hits = best_first(symbol_tiers_at(index, commit, tree=tree, ref=ref, wanted=wanted))
    paths = [] if kind else paths_at(tree, ref, words=words, path_filter=path)

    if not hits and not paths:
        return nothing(f"Nothing named or containing {query!r} at {ref}.")

    return Result(_found_lines(hits, paths, limit=limit, name=name))


class Abbreviated(NamedTuple):
    """The smbols and paths that match a query as an abbreviation."""

    hits: list[SymbolHit]
    paths: list[Place]


def _find_abbreviated(session: IndexSession, wanted: NameQuery) -> Abbreviated:
    """
    Symbols and paths that match the query as an abbreviation, for when nothing matches it more closely.
    "reqcontroller" finds "RequestController".
    """
    parent, kind, path = wanted.parent, wanted.kind, wanted.path
    letters = "".join(NAME_WORDS.findall(wanted.name)).lower()
    if len(letters) < 2:
        return Abbreviated([], [])

    symbols = session.symbols_containing_in_order(list(letters), parent=parent, kind=kind, path=path)
    hits = best_first([[hit for hit in symbols if _is_abbreviation(letters, hit.name)]])

    # A query with a dot is a file name typed in full or "Class.method", and neither is short for a path.
    if kind or any("." in word for word in wanted.words):
        return Abbreviated(hits, [])

    places = session.places_containing_in_order(list(letters))
    paths = [place for place in places if _is_abbreviation(letters, place.path) and path_matches(place.path, path)]
    return Abbreviated(hits, sorted(paths, key=lambda place: (place.is_deleted, len(place.path))))


def _is_abbreviation(letters: str, text: str) -> bool:
    """
    True when the query can be cut into pieces that each start a word of text, in order, with any words
    between. Case is ignored, so "reqcontroller" fits "getRequestController" as req and controller. It
    doesn't fit "frequencyController".
    """
    words = [word.lower() for word in NAME_WORDS.findall(text)]

    @cache
    def fits(start: int, word: int) -> bool:
        """Whether letters from start onward can be cut into word starts, using words from word onward."""
        if start == len(letters):
            return True

        for index in range(word, len(words)):
            shared = 0
            while start + shared < len(letters) and shared < len(words[index]):
                if letters[start + shared] != words[index][shared]:
                    break
                shared += 1
            if any(fits(start + length, index + 1) for length in range(shared, 0, -1)):
                return True
        return False

    return fits(0, 0)


def _find_paths(
    session: IndexSession,
    words: list[str],
    *,
    path_filter: str | None,
    scope: Scope = Scope.WORKING_TREE,
) -> list[Place]:
    """Live then deleted files whose path contains every word, shortest path first."""
    return sorted(
        (place for place in session.places_containing_all(words, scope) if path_matches(place.path, path_filter)),
        key=lambda place: (place.is_deleted, len(place.path)),
    )
