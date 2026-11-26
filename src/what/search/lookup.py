"""Picks the search for a query that came with no command: a line range, a file or a name."""

from what.dependencies import DependencyReach
from what.index import Index
from what.output import LineKind, Result
from what.search.at_ref import resolve_tree_path, tree_at
from what.search.files import outline_at_ref, outline_live, show_at_ref, show_live
from what.search.names import find_at_ref, find_live
from what.search.paths import LINE_RANGE, resolve_path
from what.symbols import Kind


def lookup(
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
    Run the search that fits the query. A line range such as "src/a.py:45-80" is shown. A query that
    names one file gives the file's outline, or its text when it defines nothing. Anything else is a name for find.
    """
    if ref is not None:
        return _lookup_at_ref(index, query, kind=kind, path=path, limit=limit, ref=ref)

    index.refresh()

    if LINE_RANGE.fullmatch(query):
        return show_live(index, query, path=path, deps=deps)

    # A plain word is always a name. Scripts like bin/refresh are often named after a function, and the function
    # is what a search for "refresh" wants.
    names_a_file = not kind and ("/" in query or "." in query)
    with index.connect() as session:
        places = resolve_path(session, query) if names_a_file else []
        defines_something = len(places) == 1 and bool(session.symbols_in_blob(places[0].blob_row))

    if len(places) != 1:
        return find_live(index, query, kind=kind, path=path, limit=limit, branches=branches, deps=deps)
    return outline_live(index, query) if defines_something else show_live(index, query, path=None, deps=deps)


def _lookup_at_ref(
    index: Index,
    query: str,
    *,
    kind: Kind | None,
    path: str | None,
    limit: int,
    ref: str,
) -> Result:
    if LINE_RANGE.fullmatch(query):
        return show_at_ref(index, query, path=path, ref=ref)

    names_a_file = not kind and ("/" in query or "." in query)
    places = resolve_tree_path(tree_at(index, ref)[1], ref, path=query) if names_a_file else []

    if len(places) != 1:
        return find_at_ref(index, query, kind=kind, path=path, limit=limit, ref=ref)

    # As in the working tree, a file that defines nothing is shown, since its outline would be empty.
    outlined = outline_at_ref(index, query, ref=ref)
    defines_something = any(line.kind == LineKind.ROW for line in outlined.lines)
    return outlined if defines_something else show_at_ref(index, query, path=None, ref=ref)
