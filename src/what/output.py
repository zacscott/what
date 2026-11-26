"""
What a search found, as lines, and the three ways the command line prints them: plain text for a pipe or a
coding harness, colour for a person at a terminal, and JSON.

A search across files prints each match as "path:line:text", the way grep does. A command about one file, outline
or show, prints the path once as a heading and then "line:text".
"""

from dataclasses import KW_ONLY, dataclass
from enum import StrEnum
from typing import NamedTuple, assert_never

from what.models import AtRef, Deleted, InPackage, Live, OnBranch, Place


class LineKind(StrEnum):
    """What one line of output is. The value is the "type" of the line in JSON."""

    # A match in a file, printed as "path:line:text".
    MATCH = "match"
    # A line near a match, or the first line of the symbol a match sits in, printed as "path-line-text".
    CONTEXT = "context"
    # A file whose path is the match, printed as "path".
    FILE = "file"
    # The file that the rows after it belong to, printed as "path" or "path:first-last".
    HEADING = "heading"
    # A line of one file under a heading, printed as "line:text" or "first-last:text".
    ROW = "row"
    # A sentence for the reader, such as how many hits there were or which branches were searched.
    NOTE = "note"
    # The "--" between two groups of lines that are not next to each other in the file.
    BREAK = "break"
    # Why the command could not run, such as a ref that doesn't exist. Only --json prints an error as a line.
    ERROR = "error"


class Mark(StrEnum):
    """What a search says about one match. Plain text shows it at the end of the line, as "  [definition]"."""

    # The line is where the symbol that matched is defined.
    DEFINITION = "definition"
    # The line may not call the symbol, such as a method called on a variable whose class isn't known.
    POSSIBLE = "possible"


class Span(NamedTuple):
    """A part of a line's text that matched, from the character at start up to the one before end."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Line:
    """
    One line of output. text is the source line, signature or sentence. line and end_line are the lines of the
    file it covers. spans are the parts of text that matched, for a terminal to highlight.
    """

    kind: LineKind
    _: KW_ONLY
    text: str = ""
    place: Place | None = None
    line: int | None = None
    end_line: int | None = None
    mark: Mark | None = None
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    """
    The output of one search. found is False when the search matched nothing, and the lines then say so. The
    command line prints those lines to stderr and exits with 1, as grep does.
    """

    lines: list[Line]
    found: bool = True

    @property
    def text(self) -> str:
        """The output as plain text."""
        return plain(self.lines)


def note(text: str) -> Line:
    """A line that is a sentence for the reader."""
    return Line(LineKind.NOTE, text=text)


def plural(count: int, one: str, many: str | None = None) -> str:
    """A count and its noun, such as "1 file" or "1,200 files". many is the plural when it is not one with an "s"."""
    return f"{count:,} {one}" if count == 1 else f"{count:,} {many or one + 's'}"


def nothing(*texts: str) -> Result:
    """The result of a search that matched nothing, with the sentences that say so."""
    return Result([note(text) for text in texts], found=False)


def plain(lines: list[Line]) -> str:
    """The lines as plain text, one line of output for each."""
    return "\n".join(_plain(line) for line in lines)


def _plain(line: Line) -> str:
    ref = line.place.ref if line.place else ""

    match line.kind:
        case LineKind.MATCH:
            return f"{ref}:{line.line}:{line.text}{_mark(line)}"
        case LineKind.CONTEXT:
            return f"{ref}-{line.line}-{line.text}"
        case LineKind.FILE:
            return ref
        case LineKind.HEADING:
            return ref if line.line is None else f"{ref}:{_range(line)}"
        case LineKind.ROW:
            return f"{_range(line)}:{line.text}"
        case LineKind.NOTE | LineKind.ERROR:
            return line.text
        case LineKind.BREAK:
            return "--"
        case _:
            assert_never(line.kind)


def _mark(line: Line) -> str:
    """What a search says about a match, as the end of its line."""
    return f"  [{line.mark}]" if line.mark else ""


def _range(line: Line) -> str:
    """The lines a heading or row covers, as "45" or "45-80"."""
    return f"{line.line}" if line.end_line is None else f"{line.line}-{line.end_line}"


# The escape codes a terminal reads as colour. ripgrep uses the same colours for the same parts.
PATH_COLOUR = "\x1b[35m"
NUMBER_COLOUR = "\x1b[32m"
MATCH_COLOUR = "\x1b[1;31m"
FAINT = "\x1b[2m"
RESET = "\x1b[0m"


def for_terminal(lines: list[Line]) -> str:
    """
    The lines for a person to read at a terminal. The matches of one file sit under its path, with a blank line
    between files, and the path, the line numbers and the matched text are coloured.
    """
    output: list[str] = []
    # The file whose matches are being printed, so its path is printed once.
    open_file: str | None = None

    for line in lines:
        ref = line.place.ref if line.place else ""
        in_a_file = line.kind in (LineKind.MATCH, LineKind.CONTEXT)

        if in_a_file and ref != open_file:
            if output:
                output.append("")
            output.append(_painted(ref, PATH_COLOUR))
            open_file = ref
        elif not in_a_file and line.kind != LineKind.BREAK and open_file is not None:
            output.append("")
            open_file = None

        match line.kind:
            case LineKind.MATCH:
                mark = _painted(_mark(line), FAINT) if line.mark else ""
                number = _painted(str(line.line), NUMBER_COLOUR)
                output.append(f"{number}:{_highlighted(line.text, line.spans)}{mark}")
            case LineKind.CONTEXT:
                output.append(f"{_painted(str(line.line), NUMBER_COLOUR)}-{line.text}")
            case LineKind.FILE:
                output.append(_painted(ref, PATH_COLOUR))
            case LineKind.HEADING:
                lines_covered = "" if line.line is None else f":{_painted(_range(line), NUMBER_COLOUR)}"
                output.append(_painted(ref, PATH_COLOUR) + lines_covered)
            case LineKind.ROW:
                output.append(f"{_painted(_range(line), NUMBER_COLOUR)}:{_highlighted(line.text, line.spans)}")
            case LineKind.NOTE | LineKind.ERROR:
                output.append(line.text)
            case LineKind.BREAK:
                output.append(_painted("--", FAINT))
            case _:
                assert_never(line.kind)

    return "\n".join(output)


def _painted(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}"


def _highlighted(text: str, spans: tuple[Span, ...]) -> str:
    """The text with each span that matched in the colour of a match. The spans are in order and don't overlap."""
    output: list[str] = []
    position = 0

    for start, end in spans:
        output.append(text[position:start])
        output.append(_painted(text[start:end], MATCH_COLOUR))
        position = end

    output.append(text[position:])
    return "".join(output)


def as_json(lines: list[Line]) -> str:
    """The lines as JSON, one object on each line of output. A break between groups of lines is left out."""
    # Only --json needs the json module, so a plain search doesn't pay for the import.
    import json

    return "\n".join(json.dumps(_record(line)) for line in lines if line.kind != LineKind.BREAK)


def _record(line: Line) -> dict[str, object]:
    """One line as the object --json prints. A key is left out when the line has no value for it."""
    record: dict[str, object] = {"type": line.kind.value}

    if line.place is not None:
        record["path"] = line.place.path
        record |= _origin(line.place)
    if line.line is not None:
        record["line"] = line.line
    if line.end_line is not None:
        record["end_line"] = line.end_line
    if line.kind not in (LineKind.FILE, LineKind.HEADING):
        record["text"] = line.text
    if line.mark:
        record["mark"] = line.mark.value
    if line.spans:
        record["spans"] = [[span.start, span.end] for span in line.spans]

    return record


def _origin(place: Place) -> dict[str, object]:
    """Where a file that is not in the working tree comes from, as the keys --json adds for it."""
    match place.origin:
        case Live():
            return {}
        case Deleted(commit=commit):
            return {"deleted_in": commit}
        case AtRef(ref=ref):
            return {"ref": ref}
        case OnBranch(name=name):
            return {"branch": name}
        case InPackage(label=label):
            return {"package": label}
        case _:
            assert_never(place.origin)
