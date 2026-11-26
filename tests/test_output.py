"""Prints the lines a search found as plain text, for a terminal and as JSON."""

import json
from pathlib import Path

from what import output
from what.models import AtRef, Deleted, InPackage, OnBranch, Place
from what.output import Line, LineKind, Mark, Span

BENDER = Place("src/Bender.php", "a" * 40, 1)
PEELER = Place("src/Peeler.php", "b" * 40, 2)

# One line of each kind, as a grep over two files and then a show would print them.
LINES = [
    output.note("2 hits in 2 files"),
    Line(LineKind.CONTEXT, text="function bend()", place=BENDER, line=3),
    Line(LineKind.MATCH, text="    return bend();", place=BENDER, line=4, mark=Mark.POSSIBLE, spans=(Span(11, 15),)),
    Line(LineKind.BREAK),
    Line(LineKind.MATCH, text="bend();", place=BENDER, line=9, spans=(Span(0, 4),)),
    Line(LineKind.MATCH, text="function bend()", place=PEELER, line=1, mark=Mark.DEFINITION),
    Line(LineKind.FILE, place=PEELER),
    Line(LineKind.HEADING, place=PEELER, line=1, end_line=2),
    Line(LineKind.ROW, text="function bend()", place=PEELER, line=1),
    Line(LineKind.ROW, text="class Peeler", place=PEELER, line=1, end_line=2),
]


def test_plain_text_is_one_line_for_each_line() -> None:
    assert output.plain(LINES).splitlines() == [
        "2 hits in 2 files",
        "src/Bender.php-3-function bend()",
        "src/Bender.php:4:    return bend();  [possible]",
        "--",
        "src/Bender.php:9:bend();",
        "src/Peeler.php:1:function bend()  [definition]",
        "src/Peeler.php",
        "src/Peeler.php:1-2",
        "1:function bend()",
        "1-2:class Peeler",
    ]


def test_a_terminal_gets_each_file_once_with_a_blank_line_between_files() -> None:
    path, number, match, faint, reset = (
        output.PATH_COLOUR,
        output.NUMBER_COLOUR,
        output.MATCH_COLOUR,
        output.FAINT,
        output.RESET,
    )

    assert output.for_terminal(LINES).splitlines() == [
        "2 hits in 2 files",
        "",
        f"{path}src/Bender.php{reset}",
        f"{number}3{reset}-function bend()",
        f"{number}4{reset}:    return {match}bend{reset}();{faint}  [possible]{reset}",
        f"{faint}--{reset}",
        f"{number}9{reset}:{match}bend{reset}();",
        "",
        f"{path}src/Peeler.php{reset}",
        f"{number}1{reset}:function bend(){faint}  [definition]{reset}",
        "",
        f"{path}src/Peeler.php{reset}",
        f"{path}src/Peeler.php{reset}:{number}1-2{reset}",
        f"{number}1{reset}:function bend()",
        f"{number}1-2{reset}:class Peeler",
    ]


def test_json_is_one_object_for_each_line_and_leaves_out_a_break() -> None:
    assert [json.loads(line) for line in output.as_json(LINES).splitlines()] == [
        {"type": "note", "text": "2 hits in 2 files"},
        {"type": "context", "path": "src/Bender.php", "line": 3, "text": "function bend()"},
        {
            "type": "match",
            "path": "src/Bender.php",
            "line": 4,
            "text": "    return bend();",
            "mark": "possible",
            "spans": [[11, 15]],
        },
        {"type": "match", "path": "src/Bender.php", "line": 9, "text": "bend();", "spans": [[0, 4]]},
        {"type": "match", "path": "src/Peeler.php", "line": 1, "text": "function bend()", "mark": "definition"},
        {"type": "file", "path": "src/Peeler.php"},
        {"type": "heading", "path": "src/Peeler.php", "line": 1, "end_line": 2},
        {"type": "row", "path": "src/Peeler.php", "line": 1, "text": "function bend()"},
        {"type": "row", "path": "src/Peeler.php", "line": 1, "end_line": 2, "text": "class Peeler"},
    ]


def test_json_says_where_a_file_outside_the_working_tree_comes_from() -> None:
    origins = [
        (Deleted("c" * 40), {"deleted_in": "c" * 40}),
        (AtRef("origin/dev"), {"ref": "origin/dev"}),
        (OnBranch("feature/peel", 0), {"branch": "feature/peel"}),
        (InPackage("vendor: acme/bender@1.0.0", Path("/vendor/acme/bender")), {"package": "vendor: acme/bender@1.0.0"}),
    ]

    for origin, keys in origins:
        line = Line(LineKind.FILE, place=Place("src/Bender.php", "a" * 40, 1, origin))

        assert json.loads(output.as_json([line])) == {"type": "file", "path": "src/Bender.php"} | keys


def test_a_count_takes_the_plural_and_a_comma_in_a_big_number() -> None:
    assert output.plural(1, "file") == "1 file"
    assert output.plural(1200, "file") == "1,200 files"
    assert output.plural(2, "other branch", "other branches") == "2 other branches"
