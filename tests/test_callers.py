"""Finds the lines that call a stmbol, leaving out its definition and the comments that name it."""

from pathlib import Path

from what import search
from what.index import Index

RIPENER = """<?php
class Ripener {
    /**
     * Call Ripener::ripen once a day.
     */
    public static function ripen(int $days): void {
        echo $days;
    }
}

class Shelf {
    public static function ripen(): void {}
}
"""


def write_callers(repo: Path) -> None:
    (repo / "src/Ripener.php").write_text(RIPENER)
    for name in ["Kitchen", "Market", "Farm"]:
        (repo / f"src/{name}.php").write_text(
            f"<?php\nfunction stock{name}() {{\n    // Ripener::ripen is slow.\n    Ripener::ripen(3);\n}}\n"
        )
    (repo / "src/Store.php").write_text("<?php\nfunction restock() {\n    Shelf::ripen();\n}\n")


def test_callers_lists_each_call_under_its_function(index: Index, repo: Path) -> None:
    write_callers(repo)

    lines = search.callers(index, "Ripener.ripen").text.splitlines()

    assert lines[0] == "3 hits in 3 files"
    assert sorted(lines[1:]) == [
        "src/Farm.php-2-function stockFarm()",
        "src/Farm.php:4:    Ripener::ripen(3);",
        "src/Kitchen.php-2-function stockKitchen()",
        "src/Kitchen.php:4:    Ripener::ripen(3);",
        "src/Market.php-2-function stockMarket()",
        "src/Market.php:4:    Ripener::ripen(3);",
    ]


def test_callers_leaves_out_the_definition_and_comments(index: Index, repo: Path) -> None:
    write_callers(repo)

    output = search.callers(index, "Ripener::ripen").text

    assert "public static function ripen" not in output
    assert "Call Ripener::ripen once a day" not in output
    assert "is slow" not in output
    assert "Shelf::ripen" not in output


def test_callers_is_certain_of_a_call_through_self_in_the_same_class(index: Index, repo: Path) -> None:
    write_callers(repo)
    (repo / "src/Ripener.php").write_text(RIPENER.replace("echo $days;", "self::ripen($days - 1);"))

    assert "src/Ripener.php:7:        self::ripen($days - 1);\n" in search.callers(index, "Ripener.ripen").text + "\n"


def test_callers_marks_a_call_it_cannot_tie_to_the_class(index: Index, repo: Path) -> None:
    write_callers(repo)
    (repo / "src/Cart.php").write_text("<?php\nfunction fill($fruit) {\n    $fruit->ripen(1);\n}\n")

    assert "src/Cart.php:3:    $fruit->ripen(1);" + "  [possible]" in search.callers(index, "Ripener.ripen").text


def test_callers_of_a_name_that_is_not_defined(index: Index) -> None:
    assert (
        search.callers(index, "Nowhere.ripen").text
        == "No symbol called 'Nowhere.ripen'. Use find to search by part of a name."
    )


def test_callers_of_a_python_function(index: Index, repo: Path) -> None:
    (repo / "src/ripe.py").write_text(
        'def ripen():\n    """Ripen the fruit. Call ripen() daily."""\n\n\ndef daily():\n    # ripen() is slow\n    ripen()\n'
    )

    assert search.callers(index, "ripen", path="ripe.py").text.splitlines() == [
        "1 hit in 1 file",
        "src/ripe.py-5-def daily():",
        "src/ripe.py:7:    ripen()",
    ]


def test_callers_keeps_a_string_only_when_it_names_the_callable(index: Index, repo: Path) -> None:
    write_callers(repo)
    (repo / "src/Queue.php").write_text(
        "<?php\nfunction queue() {\n    $name = 'fruit-ripen-queue';\n    call_user_func('Ripener::ripen', 1);\n}\n"
    )

    output = search.callers(index, "Ripener.ripen").text

    assert "fruit-ripen-queue" not in output
    assert "src/Queue.php:4:    call_user_func('Ripener::ripen', 1);" + "  [possible]" in output


def test_callers_does_not_read_the_files_after_its_limit(index: Index, repo: Path) -> None:
    write_callers(repo)

    # The third caller, the file that defines ripen and the caller of Shelf::ripen are left.
    lines = search.callers(index, "Ripener.ripen", limit=2).text.splitlines()

    assert lines[0] == "2 hits in 2 files and 3 more files not read, 2 shown. Use --limit or --path to see more."
    assert len([line for line in lines if "Ripener::ripen(3);" in line]) == 2


def test_callers_finds_a_call_far_along_a_long_line(index: Index, repo: Path) -> None:
    write_callers(repo)
    padding = "$a = 1; " * 100
    (repo / "src/Long.php").write_text(f"<?php\nfunction longLine() {{\n    {padding}Ripener::ripen(9);\n}}\n")

    output = search.callers(index, "Ripener.ripen", path="Long.php").text

    assert "src/Long.php:3:" in output
    assert "  [possible]" not in output
