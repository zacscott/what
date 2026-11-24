"""Finds the classes, functions and other named definitions in one source file."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from what.config import settings

if TYPE_CHECKING:
    import tree_sitter_language_pack as grammars

# Languages whose grammar could not be downloaded. We don't retry them for every file.
_unavailable: set[str] = set()


@dataclass(frozen=True)
class Symbol:
    """
    One definition in a file. Lines count from 1. For example:

        Symbol("bandana", "method", "BananaBender", 45, 80, "public function bandana(): string")
    """

    name: str
    kind: str
    parent: str | None
    line: int
    end_line: int
    signature: str


def split_lines(text: str) -> list[str]:
    """
    Split text into lines the way tree-sitter and git count them, on "\n" only.
    str.splitlines also splits on form feeds and other characters, which shifts every later line number.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()

    return [line.removesuffix("\r") for line in lines]


def extract(path: str, text: str) -> list[Symbol]:
    """The definitions in a file, in line order. Empty when the language isn't supported."""
    # Only a file that changed needs parsing, so the grammar pack is imported here. Importing it costs about 13ms,
    # which every search would pay.
    import tree_sitter_language_pack as grammars

    language = grammars.detect_language_from_path(path)
    if language is None or language in settings.NO_SYMBOLS or language in _unavailable:
        return []

    config = grammars.ProcessConfig(
        language=language,
        structure=True,
        symbols=True,
        imports=False,
        exports=False,
        parse_timeout_ms=settings.PARSE_TIMEOUT_MS,
    )

    try:
        parsed = grammars.process(text, config)
    except grammars.DownloadError:
        _unavailable.add(language)
        return []
    except grammars.Error:
        return []

    lines = split_lines(text)
    found: dict[tuple[str, int], Symbol] = {}
    _add_structure(parsed.structure, None, found)

    # The flat symbol list has constants and types that the structure tree leaves out.
    for symbol in parsed.symbols:
        if symbol.span is None:
            continue

        line = symbol.span.start_line + 1
        if (symbol.name, line) not in found:
            signature = lines[line - 1].strip() if line <= len(lines) else symbol.name
            found[(symbol.name, line)] = Symbol(
                symbol.name,
                str(symbol.kind).lower(),
                _enclosing(found, line),
                line,
                symbol.span.end_line + 1,
                signature[: settings.MAX_SIGNATURE_LENGTH],
            )

    return sorted(found.values(), key=lambda symbol: (symbol.line, -symbol.end_line))


def _add_structure(
    items: list[grammars.StructureItem],
    parent: str | None,
    found: dict[tuple[str, int], Symbol],
) -> None:
    for item in items:
        if item.name and item.span is not None:
            line = item.span.start_line + 1

            # The grammar pack's signature can run on into a comment on the next line.
            code_lines = [
                line for line in (item.signature or item.name).splitlines() if not line.lstrip().startswith(("#", "//"))
            ]
            signature = " ".join(" ".join(code_lines).split())

            found[(item.name, line)] = Symbol(
                item.name,
                str(item.kind).lower(),
                parent,
                line,
                item.span.end_line + 1,
                signature[: settings.MAX_SIGNATURE_LENGTH],
            )

        _add_structure(item.children, item.name or parent, found)


def _enclosing(found: dict[tuple[str, int], Symbol], line: int) -> str | None:
    """The name of the smallest symbol that starts before a line and ends after it."""
    around = [symbol for symbol in found.values() if symbol.line < line <= symbol.end_line]
    if not around:
        return None
    return min(around, key=lambda symbol: symbol.end_line - symbol.line).name
