"""Finds the symbols that match a name, in the index and in installed dependencies, and puts the best first."""

import re
from typing import Final, NamedTuple

from what import dependencies
from what.database import IndexSession
from what.dependencies import DependencyReach
from what.index import Index
from what.models import NameQuery, Scope, SymbolHit
from what.symbols import Kind

TYPE_KINDS: Final = {
    Kind.CLASS,
    Kind.INTERFACE,
    Kind.TRAIT,
    Kind.ENUM,
    Kind.STRUCT,
    Kind.TYPE,
    Kind.MODULE,
    Kind.NAMESPACE,
    Kind.IMPL,
}


CALLABLE_KINDS: Final = {Kind.FUNCTION, Kind.METHOD}


class PackageHits(NamedTuple):
    """Symbols in the installed dependencies, in the tiers find ranks, and a note for each incomplete lockfile."""

    tiers: list[list[SymbolHit]]
    notes: list[str]


def dependency_symbols(
    index: Index,
    wanted: NameQuery,
    *,
    deps: DependencyReach,
) -> PackageHits:
    """
    Symbols in the installed dependencies that match a name, in the tiers find ranks, and a note for each lockfile
    with packages that aren't installed.
    """
    installed = dependencies.installed(index, transitive=deps == DependencyReach.ALL)
    packages = dependencies.package_rows(index, installed.packages)
    if not packages:
        return PackageHits([], list(installed.notes))

    return PackageHits(dependencies.find_symbols(index, packages, query=wanted), list(installed.notes))


class Qualified(NamedTuple):
    """A name split into its class, or None when it names no class, and the name itself."""

    parent: str | None
    name: str


def split_qualified(name: str) -> Qualified:
    """
    Split "Parent::name", "Parent.name" or "Parent#name" into its two parts. The index keeps a class's name without
    its namespace, so "Phinx\\Db\\Table.addIndex" gives "Table" and "addIndex".
    """
    parts = re.split(r"::|[.#]", name)
    if len(parts) >= 2 and all(parts[-2:]):
        return Qualified(parts[-2].rsplit("\\", 1)[-1], parts[-1])
    return Qualified(None, name.rsplit("\\", 1)[-1])


def symbols_matching(
    session: IndexSession,
    wanted: NameQuery,
    *,
    scope: Scope = Scope.WORKING_TREE,
) -> list[SymbolHit]:
    """Symbols matching a name, best first: exact, then prefix, then containing every word."""
    return best_first(symbol_tiers(session, wanted, scope=scope))


def symbol_tiers(
    session: IndexSession,
    wanted: NameQuery,
    *,
    scope: Scope,
) -> list[list[SymbolHit]]:
    """Symbols matching a name, as the three tiers find ranks: exact, then prefix, then containing every word."""
    name, parent, kind, path = wanted.name, wanted.parent, wanted.kind, wanted.path
    return [
        session.symbols_named(name, parent=parent, kind=kind, path=path, scope=scope),
        session.symbols_starting_with(name, parent=parent, kind=kind, path=path, scope=scope),
        session.symbols_containing_all(list(wanted.words), parent=parent, kind=kind, path=path, scope=scope),
    ]


def best_first(tiers: list[list[SymbolHit]]) -> list[SymbolHit]:
    """
    Ran ksymbol hits best first. A hit from an earlier tier ranks first, and a hit that is in several tiers
    keeps its earliest.
    """
    ranked: dict[tuple[str, int, str, int], tuple[tuple[int, bool, int, bool, int], SymbolHit]] = {}
    for tier, hits in enumerate(tiers):
        for hit in hits:
            # Two branches can hold different versions of one path, so the content is part of the key.
            key = (hit.place.path, hit.place.blob_row, hit.name, hit.line)
            if key in ranked:
                continue
            kind_rank = 0 if hit.kind in TYPE_KINDS else 1 if hit.kind in CALLABLE_KINDS else 2
            rank = (tier, hit.place.is_deleted, kind_rank, _is_secondary(hit.place.path), len(hit.place.path))
            ranked[key] = (rank, hit)

    return [hit for _, hit in sorted(ranked.values(), key=lambda entry: entry[0])]


def _is_secondary(path: str) -> bool:
    """Tests and third-party code rank below the code a search is most likely after."""
    lowered = path.lower()
    return any(part in lowered for part in ("test", "spec", "vendor/", "node_modules/", "fixture"))
