"""Finds the lines that call or reference a symbol."""

import re
from bisect import bisect_right
from typing import Final

from what.index import Index
from what.models import (
    SymbolHit,
    path_matches,
)
from what.output import Mark, Result, note, nothing
from what.search.lines import innermost, lines_under_symbols
from what.search.paths import dependency_paths
from what.search.ranking import TYPE_KINDS, split_qualified
from what.search.text import TextMatches
from what.symbols import Role, split_lines, text_roles

# What comes before a name on its line when something holds it: the holder and the operator, such as "$this->",
# "Deepgram::", "self." or a namespace like "App\".
RECEIVER: Final = re.compile(r"([\w$\\]*)\s*(::|\?->|->|\?\.|\.|\\)\s*$")


# How many characters before a match are read for what holds the name.
# On a long line, the pattern would otherwise try every position.
RECEIVER_WINDOW: Final = 200


def callers(
    index: Index,
    target: str,
    *,
    path: str | None = None,
    limit: int = 40,
) -> Result:
    """
    The lines that call or reference a symbol, each under the symbol it sits in. target is a name or
    "Class.method". The definitions, and mentions in comments, are left out. A line that may not call this symbol,
    such as a method called on a variable whose class isn't known, is marked Mark.POSSIBLE.
    """
    index.refresh()
    parent, name = split_qualified(target)

    with index.connect() as session:
        named = [hit for hit in session.symbols_named(name, parent=None, kind=None, path=None) if hit.place.is_live]
        targets = [hit for hit in named if not parent or (hit.parent or "").lower() == parent.lower()]
        if not targets:
            return nothing(f"No symbol called {target!r}. Use find to search by part of a name.")

        # A method name given without its class stands for that class when only one class defines it.
        owners = {hit.parent for hit in targets}
        owner = parent or (owners.pop() if len(owners) == 1 else None)
        is_member = any(hit.parent and hit.kind not in TYPE_KINDS for hit in targets)
        definitions = {(hit.place.path, hit.line) for hit in named}

        matcher = re.compile(rf"(?<![\w$]){re.escape(name)}(?!\w)")
        literals = [name] if len(name) >= 3 else []
        in_dependencies = dependency_paths(index)
        live = [
            place
            for place in session.live_places_with_text(literals)
            if path_matches(place.path, path) and not in_dependencies(place.path)
        ]

        found = TextMatches([])
        for read, place in enumerate(live):
            # Each file with a call is parsed to tell code from comments, so the files after the limit are not read.
            if found.shown >= limit:
                found.unread = len(live) - read
                break

            text = index.read_live(place.path)
            if text is None:
                continue

            symbols = session.symbols_in_blob(place.blob_row)
            calls = _calls_in(
                place.path,
                text,
                matcher=matcher,
                definitions=definitions,
                owner=owner,
                is_member=is_member,
                symbols=symbols,
            )
            if not calls:
                continue

            found.hits += len(calls)
            found.files += 1
            shown = calls[: limit - found.shown]
            marks = {number: Mark.POSSIBLE for number, _, certain in shown if not certain}
            found.lines.extend(
                lines_under_symbols(
                    place, symbols, matches=[(n, line) for n, line, _ in shown], pattern=matcher, marks=marks
                )
            )
            found.shown += len(shown)

    if not found.lines:
        return nothing(f"Nothing calls or references {target!r} in live files.")

    return Result([note(found.summary()), *found.lines])


def _calls_in(
    path: str,
    text: str,
    *,
    matcher: re.Pattern[str],
    definitions: set[tuple[str, int]],
    owner: str | None,
    is_member: bool,
    symbols: list[SymbolHit],
) -> list[tuple[int, str, bool]]:
    """
    The lines of one file that call or reference the symbol, as (line number, line, certain). A line with several
    matches takes the most certain one.
    """
    # The pattern runs once over the whole file. A name never spans two lines, so each match is on one line.
    found = list(matcher.finditer(text))
    if not found:
        return []

    lines = split_lines(text)
    starts = _line_starts(text)
    matches: list[tuple[int, int, int]] = []
    for match in found:
        number = bisect_right(starts, match.start())
        if (path, number) not in definitions:
            start = starts[number - 1]
            matches.append((number, match.start() - start, match.end() - start))

    roles = text_roles(path, text, positions=[(number, column + 1) for number, column, _ in matches])
    types = [symbol for symbol in symbols if symbol.kind in TYPE_KINDS]

    best: dict[int, bool] = {}
    for (number, column, end), role in zip(matches, roles):
        if role == Role.COMMENT:
            continue
        if role == Role.STRING and not _names_a_callable(lines[number - 1], start=column, end=end):
            continue

        enclosing = innermost(types, number)
        certainty = _call_certainty(
            lines[number - 1][max(0, column - RECEIVER_WINDOW) : column],
            owner=owner,
            is_member=is_member,
            enclosing=enclosing.name if enclosing else None,
        )
        if certainty is not None:
            best[number] = best.get(number, False) or (certainty and role == Role.CODE)

    return [(number, lines[number - 1], certain) for number, certain in sorted(best.items())]


# A string that names a callable ends at the name, and has nothing before the name but a class and "::", ".",
# "@" or "->", as in 'Deepgram::transcribe', 'transcribe' or [$this, 'transcribe'].
CALLABLE_STRING: Final = re.compile(r"""["'][\w\\]*(::|\.|@|->)?$""")


def _names_a_callable(line: str, *, start: int, end: int) -> bool:
    """
    True when the match from start to end, inside a string, is a callable's name, such as 'Deepgram::transcribe',
    and not a word in text, such as 'ai-scribe-transcribe-chunk'.
    """
    before = line[max(0, start - RECEIVER_WINDOW) : start]
    return bool(CALLABLE_STRING.search(before)) and line[end : end + 1] in ("'", '"')


def _call_certainty(
    before: str,
    *,
    owner: str | None,
    is_member: bool,
    enclosing: str | None,
) -> bool | None:
    """
    Whether a match is certainly the symbol (True), may be it (False) or is not it (None). before is the text before
    the match on its line, owner the class that defines the symbol and enclosing the class the match sits in.
    """
    receiver = RECEIVER.search(before)
    if receiver is None:
        # A method is called through sonething that holds it. A bare name of a method is likely a function.
        return not is_member

    holder, operator = receiver.groups()
    holder = holder.rsplit("\\", 1)[-1].lower()
    if operator == "\\":
        # A namespace, as in App\Deepgram.
        return True
    if not is_member:
        # A function called through something, such as module.function(), may be another symbol with that name.
        return False

    if holder in {"$this", "this", "self", "static"}:
        return owner is not None and enclosing is not None and enclosing.lower() == owner.lower()
    if owner is not None and holder == owner.lower():
        return True
    if holder == "parent":
        return False
    if operator == "::" or (operator == "." and receiver.group(1)[:1].isupper()):
        # A static call on another class.
        return None

    return False


def _line_starts(text: str) -> list[int]:
    """The position in text where each line starts, for finding the line a position is on."""
    starts = [0]
    position = text.find("\n")
    while position != -1:
        starts.append(position + 1)
        position = text.find("\n", position + 1)

    return starts
