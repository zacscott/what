"""Reads the files and symbols of a branch, tag or commit, which the index doesn't hold."""

from typing import NamedTuple

from what import git
from what.config import settings
from what.index import Index, parsed_as
from what.models import (
    AtRef,
    NameQuery,
    Place,
    RefFile,
    Scope,
    SymbolHit,
    path_matches,
)
from what.search.ranking import symbol_tiers
from what.symbols import Kind, Symbol, extract


class RefTree(NamedTuple):
    """The commit a ref names, and every file at it with its blob id."""

    commit: str
    files: dict[str, str]


def tree_at(index: Index, ref: str) -> RefTree:
    """The commit a ref names, and every file at it with its blob id. Raises git.UnknownRef when there is no commit."""
    if not index.in_git:
        raise git.UnknownRef(f"--ref needs a git repository, and {index.root} is not one")

    commit = git.commit_of(index.root, ref)
    return RefTree(commit, git.files_at(index.root, commit))


def resolve_tree_path(
    tree: dict[str, str],
    ref: str,
    *,
    path: str,
) -> list[Place]:
    """The file a path names at a rf, the way resolve_path finds it in the working tree."""
    path = path.removeprefix(ref + ":").removeprefix("./")
    if path in tree:
        return [Place(path, tree[path], 0, AtRef(ref))]

    return [Place(known, oid, 0, AtRef(ref)) for known, oid in tree.items() if known.endswith("/" + path)]


def paths_at(
    tree: dict[str, str],
    ref: str,
    *,
    words: list[str],
    path_filter: str | None,
) -> list[Place]:
    """Files at a ref whose path contains every word, shortest path first."""
    found = [
        Place(path, oid, 0, AtRef(ref))
        for path, oid in tree.items()
        if all(word.lower() in path.lower() for word in words) and path_matches(path, path_filter)
    ]
    return sorted(found, key=lambda place: len(place.path))


def symbol_tiers_at(
    index: Index,
    commit: str,
    *,
    tree: dict[str, str],
    ref: str,
    wanted: NameQuery,
) -> list[list[SymbolHit]]:
    """
    Symbols at a ref matching a name, as the three tiers find ranks. The index holds a content once, whichever
    branch it is on, so it answers for every file at the ref whose content it has seen. The other files are read
    from git and parsed.
    """
    with index.connect() as session:
        unindexed = session.load_ref_files(ref, [RefFile(path, oid, parsed_as(path)) for path, oid in tree.items()])
        tiers = symbol_tiers(session, wanted, scope=Scope.REF)

    parsed = [
        hit
        for hit in _parsed_symbols_at(
            index, commit, tree=tree, ref=ref, paths=unindexed, text=max(wanted.words, key=len), path_filter=wanted.path
        )
        if _parent_and_kind_match(hit, parent=wanted.parent, kind=wanted.kind)
    ]
    name = wanted.name.lower()
    words = [word.lower() for word in wanted.words]

    return [
        tiers[0] + [hit for hit in parsed if hit.name.lower() == name],
        tiers[1] + [hit for hit in parsed if hit.name.lower().startswith(name)],
        tiers[2] + [hit for hit in parsed if all(word in hit.name.lower() for word in words)],
    ]


def _parsed_symbols_at(
    index: Index,
    commit: str,
    *,
    tree: dict[str, str],
    ref: str,
    paths: set[str],
    text: str,
    path_filter: str | None,
) -> list[SymbolHit]:
    """
    Every symbol in those of the given files at a ref that contain text. git grep picks the files, so a search
    parses only them. Only the first settings.GIT_FILES_READ are parsed, shortest path first, so a common word
    can't make one search parse the whole tree.
    """
    places = sorted(
        (
            Place(path, tree[path], 0, AtRef(ref))
            for path in git.paths_containing(index.root, commit, text=text, among=paths)
            if path_matches(path, path_filter)
        ),
        key=lambda place: len(place.path),
    )[: settings.GIT_FILES_READ]
    hits: list[SymbolHit] = []

    for place, (_, content) in zip(places, git.read_blobs(index.root, [place.oid for place in places])):
        if content is not None and len(content) <= settings.MAX_BYTES:
            symbols = extract(place.path, content.decode("utf-8", "replace"))
            hits.extend(as_hit(place, symbol) for symbol in symbols)

    return hits


def as_hit(place: Place, symbol: Symbol) -> SymbolHit:
    return SymbolHit(place, symbol.name, symbol.kind, symbol.parent, symbol.line, symbol.end_line, symbol.signature)


def _parent_and_kind_match(hit: SymbolHit, *, parent: str | None, kind: Kind | None) -> bool:
    """The filters the index applies in SQL, for symbols that were parsed without the index."""
    if parent and (hit.parent or "").lower() != parent.lower():
        return False
    return not kind or hit.kind == kind
