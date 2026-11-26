"""Builds the output lines for the matches in one file, each under the symbol it sits in."""

import re
from collections.abc import Collection, Iterable
from typing import Final, NamedTuple

from what.config import settings
from what.models import (
    Place,
    SymbolHit,
)
from what.output import Line, LineKind, Mark, Span

# Starts a matching line whose first characters are left out, because the match is far along a very long line.
CUT_BEFORE: Final = "..."


def lines_under_symbols(
    place: Place,
    symbols: list[SymbolHit],
    *,
    matches: list[tuple[int, str]],
    pattern: re.Pattern[str],
    marks: dict[int, Mark] | None = None,
    source: list[str] | None = None,
    context: int = 0,
    left_out: Collection[int] = (),
) -> list[Line]:
    """
    One file's matching lines, in line order, each with its indentation. The signature of each symbol a match sits
    in comes before it as a context line, the way grep and ripgrep print context.

    pattern is what matched, for a terminal to highlight. marks says what is known about some matches, by line
    number. source is every line of the file, and context is how many lines before and after each match are
    shown from it. With context, a break comes wherever two lines of output are not next to each other in the file,
    and every context line is the source line as it is, so a signature over several lines is not also shown joined.
    left_out holds the lines that match but are past the limit. They are not shown as context, because a context
    line is one that does not match.
    """
    texts = dict(matches)
    with_context = source is not None and context > 0
    near = set(texts)
    if source and with_context:
        near = _lines_near(texts, context=context, last_line=len(source), left_out=set(left_out))
    # The signature of each symbol a match sits in, by the line the symbol starts on.
    headings = {symbol.line: symbol.signature for number in texts if (symbol := innermost(symbols, number))}

    output: list[Line] = []
    previous: int | None = None

    for number in sorted(near | headings.keys()):
        if with_context and previous is not None and number > previous + 1:
            output.append(Line(LineKind.BREAK))
        previous = number

        if number in texts:
            text, spans = _match_text(texts[number], pattern)
            mark = (marks or {}).get(number)
            output.append(Line(LineKind.MATCH, text=text, place=place, line=number, mark=mark, spans=spans))
        elif source and with_context:
            output.append(Line(LineKind.CONTEXT, text=_cut(source[number - 1]), place=place, line=number))
        else:
            output.append(Line(LineKind.CONTEXT, text=headings[number], place=place, line=number))

    return output


def _lines_near(
    numbers: Iterable[int],
    *,
    context: int,
    last_line: int,
    left_out: Collection[int],
) -> set[int]:
    """
    The given line numbers and the lines within context lines of each, in a file of last_line lines. The lines
    before and after a number stop at a line in left_out, so every line stays next to the one it is context for.
    """
    near: set[int] = set()

    for number in numbers:
        near.add(number)
        for step in (-1, 1):
            for distance in range(1, context + 1):
                line = number + step * distance
                if line < 1 or line > last_line or line in left_out:
                    break
                near.add(line)

    return near


def _cut(line: str) -> str:
    """A source line as output shows it: with its indentation, and cut when it is very long."""
    return line.rstrip()[: settings.MAX_LINE_LENGTH]


class MatchText(NamedTuple):
    """A matching line as output shows it, and where the pattern matches in that text."""

    text: str
    spans: tuple[Span, ...]


def _match_text(line: str, pattern: re.Pattern[str]) -> MatchText:
    """
    A matching line as output shows it, and where the pattern matches in it, for a terminal to highlight. A very
    long line is cut. When its first match is past the cut, the text starts shortly before the match and after
    CUT_BEFORE, so the match is always shown.
    """
    line = line.rstrip()
    first = pattern.search(line)
    start = 0
    if first is not None and first.end() > settings.MAX_LINE_LENGTH:
        start = max(0, first.start() - settings.MATCH_LEAD)

    lead = CUT_BEFORE if start else ""
    text = lead + line[start : start + settings.MAX_LINE_LENGTH]
    # Where a position in the line is in the text. A match is found in the whole line, so one that the cut goes
    # through still has a span, which ends where the text does.
    shift = len(lead) - start
    spans: list[Span] = []

    for match in pattern.finditer(line):
        if match.start() + shift >= len(text):
            break
        span = Span(max(match.start() + shift, len(lead)), min(match.end() + shift, len(text)))
        if span.end > span.start:
            spans.append(span)

    return MatchText(text, tuple(spans))


def innermost(symbols: list[SymbolHit], line: int) -> SymbolHit | None:
    around = [symbol for symbol in symbols if symbol.line <= line <= symbol.end_line]
    return min(around, key=lambda symbol: symbol.end_line - symbol.line) if around else None
