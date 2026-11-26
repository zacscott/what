"""Prints one file: what it defines, or the source of some of its lines or of one symbol."""

from what.config import settings
from what.dependencies import DependencyReach
from what.index import Index
from what.models import (
    AtRef,
    NameQuery,
    OnBranch,
    Place,
    SymbolHit,
)
from what.output import Line, LineKind, Result, note, nothing, plural
from what.search.at_ref import as_hit, resolve_tree_path, symbol_tiers_at, tree_at
from what.search.paths import LINE_RANGE, dependency_paths, path_choices, read_place, resolve_path
from what.search.ranking import best_first, dependency_symbols, split_qualified, symbols_matching
from what.symbols import extract, split_lines


def outline(
    index: Index,
    path: str,
    *,
    ref: str | None = None,
) -> Result:
    """Every symbol in one file with its line range, indented by nesting. With a ref, the file is read at that ref."""
    if ref is not None:
        return outline_at_ref(index, path, ref=ref)

    index.refresh()
    return outline_live(index, path)


def outline_live(index: Index, path: str) -> Result:
    with index.connect() as session:
        places = resolve_path(session, path)
        if len(places) != 1:
            return path_choices(path, places)
        symbols = session.symbols_in_blob(places[0].blob_row)

    return _outline_lines(places[0], symbols)


def outline_at_ref(
    index: Index,
    path: str,
    *,
    ref: str,
) -> Result:
    places = resolve_tree_path(tree_at(index, ref)[1], ref, path=path)
    if len(places) != 1:
        return path_choices(path, places)

    text = read_place(index, places[0])
    symbols = extract(places[0].path, text) if text is not None else []
    return _outline_lines(places[0], [as_hit(places[0], symbol) for symbol in symbols])


def _outline_lines(place: Place, symbols: list[SymbolHit]) -> Result:
    """The file as a heading, then each symbol as "first-last:signature", indented by how deep it is nested."""
    output = [Line(LineKind.HEADING, place=place)]
    if not symbols:
        return Result([*output, note("(no symbols found; use show to read it)")])

    open_ends: list[int] = []
    for hit in symbols:
        while open_ends and hit.line > open_ends[-1]:
            open_ends.pop()
        signature = "  " * len(open_ends) + hit.signature
        output.append(Line(LineKind.ROW, text=signature, place=place, line=hit.line, end_line=hit.end_line))
        open_ends.append(hit.end_line)

    return Result(output)


def show(
    index: Index,
    target: str,
    *,
    path: str | None = None,
    ref: str | None = None,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> Result:
    """
    The source of one symbol, a line range or a whole file. target is a symbol
    name ("bandana", "BananaBender.bandana"), "path:45-80" or a path. Deleted
    files are read from git. With a ref, the source is read at that ref. deps
    says when a symbol is looked for in the installed dependencies.
    """
    if ref is not None:
        return show_at_ref(index, target, path=path, ref=ref)

    index.refresh()
    return show_live(index, target, path=path, deps=deps)


def show_live(
    index: Index,
    target: str,
    *,
    path: str | None,
    deps: DependencyReach,
) -> Result:
    in_dependencies = dependency_paths(index)
    with index.connect() as session:
        line_range = LINE_RANGE.fullmatch(target)
        places = resolve_path(session, line_range.group(1) if line_range else target)

        if line_range and len(places) != 1:
            return path_choices(line_range.group(1), places)

        if len(places) == 1:
            first = int(line_range.group(2)) if line_range else 1
            last = int(line_range.group(3) or first) if line_range else None
            return _source(index, places[0], first=first, last=last)

        parent, name = split_qualified(target)
        wanted = NameQuery(name, (name,), parent, None, path)
        hits = [
            hit
            for hit in symbols_matching(session, wanted)
            if hit.name.lower() == name.lower() and not in_dependencies(hit.place.path)
        ]

    notes: list[str] = []
    if deps != DependencyReach.FALLBACK or not hits:
        package_hits, notes = dependency_symbols(index, wanted, deps=deps)
        # When dependencies were asked for, their source is what the reader wants to see first.
        hits = (
            best_first(package_hits[:1]) + hits
            if deps != DependencyReach.FALLBACK
            else hits + best_first(package_hits[:1])
        )

    if not hits:
        if len(places) > 1:
            return path_choices(target, places)
        return nothing(f"No symbol or file called {target!r}. Use find to search by part of a name.", *notes)

    shown = _show_hits(index, hits)
    return Result([*shown.lines, *map(note, notes)], found=shown.found)


def show_at_ref(
    index: Index,
    target: str,
    *,
    path: str | None,
    ref: str,
) -> Result:
    commit, tree = tree_at(index, ref)
    line_range = LINE_RANGE.fullmatch(target)
    places = resolve_tree_path(tree, ref, path=line_range.group(1) if line_range else target)

    if line_range and len(places) != 1:
        return path_choices(line_range.group(1), places)

    if len(places) == 1:
        first = int(line_range.group(2)) if line_range else 1
        last = int(line_range.group(3) or first) if line_range else None
        return _source(index, places[0], first=first, last=last)

    parent, name = split_qualified(target)
    wanted = NameQuery(name, (name,), parent, None, path)
    # Only the first tier, the symbols with exactly this name, can be the one to show.
    hits = best_first(symbol_tiers_at(index, commit, tree=tree, ref=ref, wanted=wanted)[:1])

    if not hits:
        if len(places) > 1:
            return path_choices(target, places)
        return nothing(f"No symbol or file called {target!r} at {ref}.")

    return _show_hits(index, hits)


def _show_hits(index: Index, hits: list[SymbolHit]) -> Result:
    """The source of the best hit, and where the others are."""
    best = hits[0]
    shown = _source(index, best.place, first=best.line, last=best.end_line)
    if len(hits) == 1:
        return shown

    others = [Line(LineKind.MATCH, text=hit.signature, place=hit.place, line=hit.line) for hit in hits[1:6]]
    return Result([*shown.lines, note("Also defined at (use --path to pick one):"), *others], found=shown.found)


def _source(index: Index, place: Place, *, first: int, last: int | None) -> Result:
    """
    Lines first to last of a file, under a "path:first-last" heading, each with its line number. last=None means
    to the end.
    """
    text = read_place(index, place)
    if text is None:
        return nothing(f"{place.ref} can't be shown: it is binary, larger than 1MB, or no longer in git.")

    lines = split_lines(text)
    if last is not None and last < first:
        return nothing(f"{first}-{last} is not a range of lines, because it ends before it starts.")

    # Lines are counted from 1, so line 0 is taken as the first line.
    first = max(1, first)
    last = max(1, last) if last is not None else None
    if first > max(1, len(lines)):
        return nothing(f"{place.ref} has {plural(len(lines), 'line')}, so it has no line {first}.")
    if not lines:
        return Result([Line(LineKind.HEADING, place=place), note("(the file is empty)")])

    last = min(last or len(lines), len(lines))
    shown_last = min(last, first + settings.SHOW_MAX_LINES - 1)
    output = [Line(LineKind.HEADING, place=place, line=first, end_line=shown_last)]
    output.extend(
        Line(LineKind.ROW, text=line, place=place, line=number)
        for number, line in enumerate(lines[first - 1 : shown_last], first)
    )

    if shown_last < last:
        rest = _show_command(place, first=shown_last + 1, last=last)
        output.append(note(f"({last - shown_last} more lines. Continue with {rest})"))

    return Result(output)


def _show_command(place: Place, *, first: int, last: int) -> str:
    """The command that shows these lines of a file. A file at a ref or on a branch is named with --ref."""
    match place.origin:
        case AtRef(ref=ref) | OnBranch(name=ref):
            return f"show {place.path}:{first}-{last} --ref {ref}"
        case _:
            return f"show {place.ref}:{first}-{last}"
