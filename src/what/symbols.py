"""Finds the classes, functions and other named definitions in one source file."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING

from what import log
from what.config import settings

if TYPE_CHECKING:
    from concurrent.futures import ProcessPoolExecutor

    import tree_sitter_language_pack as grammars
    from tree_sitter import Node

logger = logging.getLogger(__name__)

# Languages whose grammar could not be downloaded. We don't retry them for every file.
_unavailable: set[str] = set()


class Kind(StrEnum):
    """What a symbol is. These are the kinds the tree-sitter grammar pack reports."""

    CLASS = "class"
    CONSTANT = "constant"
    ENUM = "enum"
    FUNCTION = "function"
    IMPL = "impl"
    INTERFACE = "interface"
    METHOD = "method"
    MODULE = "module"
    NAMESPACE = "namespace"
    STRUCT = "struct"
    TRAIT = "trait"
    TYPE = "type"
    VARIABLE = "variable"
    OTHER = "other"


class Role(StrEnum):
    """What a position in a file sits in."""

    CODE = "code"
    COMMENT = "comment"
    STRING = "string"


@dataclass(frozen=True, slots=True)
class Symbol:
    """
    One definition in a file. Lines count from 1. For example:

        Symbol("bandana", "method", "BananaBender", 45, 80, "public function bandana(): string")
    """

    name: str
    kind: Kind
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
    except grammars.DownloadError as error:
        _mark_unavailable(language, error)
        return []
    except grammars.Error:
        return []

    lines = split_lines(text)
    found: dict[tuple[str, int], Symbol] = {}
    _add_structure(parsed.structure, parent=None, found=found)

    # The flat symbol list has constants and types that the structure tree leaves out.
    for symbol in parsed.symbols:
        if symbol.span is None:
            continue

        line = symbol.span.start_line + 1
        if (symbol.name, line) not in found:
            signature = lines[line - 1].strip() if line <= len(lines) else symbol.name
            found[(symbol.name, line)] = Symbol(
                symbol.name,
                _kind(symbol.kind),
                _enclosing(found, line),
                line,
                symbol.span.end_line + 1,
                signature[: settings.MAX_SIGNATURE_LENGTH],
            )

    return sorted(found.values(), key=lambda symbol: (symbol.line, -symbol.end_line))


def text_roles(
    path: str,
    text: str,
    *,
    positions: list[tuple[int, int]],
) -> list[Role]:
    """
    What each position in a file sits in: "comment", "string" or "code". A position is (line, column), both counted
    from 1 and in characters. A string that is a statement on its own, suh as a Python docstring, counts as a comment.
    Every position is "code" when the language can't be parsed.
    """
    if not positions:
        return []

    import tree_sitter_language_pack as grammars

    language = grammars.detect_language_from_path(path)
    if language is None or language in settings.NO_SYMBOLS or language in _unavailable:
        return [Role.CODE] * len(positions)

    try:
        tree = grammars.get_parser(language).parse(text.encode())
    except grammars.DownloadError as error:
        _mark_unavailable(language, error)
        return [Role.CODE] * len(positions)
    except grammars.Error:
        return [Role.CODE] * len(positions)

    lines = split_lines(text)
    roles: list[Role] = []

    for line, column in positions:
        # tree-sitter counts rows from 0 and columns in bytes.
        start = (line - 1, len(lines[line - 1][: column - 1].encode()))
        node = tree.root_node.descendant_for_point_range(start, start)
        roles.append(_role(node))

    return roles


def _role(node: Node | None) -> Role:
    """Whether a syntax node is in a comment, in a string or in code, from the nodes around it."""
    while node is not None:
        if "comment" in node.type:
            return Role.COMMENT
        if "string" in node.type:
            return Role.COMMENT if _is_lone_string(node) else Role.STRING
        node = node.parent

    return Role.CODE


def _is_lone_string(node: Node) -> bool:
    """True when the string is a statement of its own, which does nothing at runtime, like a docstring."""
    outer = node
    while outer.parent is not None and "string" in outer.parent.type:
        outer = outer.parent

    return outer.parent is not None and outer.parent.type in {"expression_statement", "block", "module"}


def _mark_unavailable(language: str, error: Exception) -> None:
    """Stop trying a language whose grammar could not be downloaded, and log it, since its files get no symbols."""
    _unavailable.add(language)
    logger.warning("no grammar for %s, so its files have no symbols: %s", language, error)


def extract_many(files: list[tuple[str, str]]) -> list[list[Symbol]]:
    """
    The definitions in each file, given as (path, text), in the same order. A batch of settings.PARALLEL_FILES or
    more is parsed in other processes, one for each core, since tree-sitter uses one core per parse.
    """
    if len(files) < settings.PARALLEL_FILES:
        return [extract(path, text) for path, text in files]

    return list(_workers().map(_extract_file, files, chunksize=8))


def _extract_file(file: tuple[str, str]) -> list[Symbol]:
    return extract(*file)


@cache
def _workers() -> ProcessPoolExecutor:
    """
    The processes that parse files, started once and kept for the rest of the run. They are started fresh, not
    forked, because forking a process that has open database connections and threads isn't safe.
    """
    # Only a big batch needs other processes, so these are imported here. Importing them costs about 6ms, which
    # every search would pay.
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    return ProcessPoolExecutor(mp_context=multiprocessing.get_context("spawn"), initializer=start_worker)


def start_worker() -> None:
    """
    Set up a process that parses files. It logs to the same file as the command. It leaves Ctrl-C to the command,
    which a terminal sends it to as well, or each worker would print a traceback of its own.
    """
    # Only a worker needs this module, so a search doesn't pay for the import.
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    log.start()


def _kind(value: object) -> Kind:
    """
    The kind of a symbol the grammar pack found. The pack writes a kind it has no name for as JSON, such as
    {"other": "constant"}, so the name inside is used when it is one of ours.
    """
    text = str(value).lower()
    if text.startswith("{"):
        text = str(json.loads(text).get("other", Kind.OTHER))

    # A kind outside the list, such as a macro, is kept as other, which a search can still find by name.
    return Kind(text) if text in Kind else Kind.OTHER


def _add_structure(
    items: list[grammars.StructureItem],
    *,
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
            signature = _on_one_line(code_lines)

            found[(item.name, line)] = Symbol(
                item.name,
                _kind(item.kind),
                parent,
                line,
                item.span.end_line + 1,
                signature[: settings.MAX_SIGNATURE_LENGTH],
            )

        _add_structure(item.children, parent=item.name or parent, found=found)


def _on_one_line(lines: list[str]) -> str:
    """
    A signature written over several lines, joined the way it would be written on one. For example
    "def bend(", "    count: int,", ") -> str:" gives "def bend(count: int) -> str:".
    """
    signature = ""
    for line in (" ".join(line.split()) for line in lines):
        if not line:
            continue

        if not signature or signature.endswith(("(", "[")):
            signature += line
        elif line.startswith((")", "]")):
            # The comma after the last parameter is only there because the parameters are on lines of their own.
            signature = signature.removesuffix(",") + line
        else:
            signature += " " + line

    return signature


def _enclosing(found: dict[tuple[str, int], Symbol], line: int) -> str | None:
    """The name of the smallest symbol that starts before a line and ends after it."""
    around = [symbol for symbol in found.values() if symbol.line < line <= symbol.end_line]
    if not around:
        return None
    return min(around, key=lambda symbol: symbol.end_line - symbol.line).name
