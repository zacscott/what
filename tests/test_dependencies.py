"""Searches that reacch the dependencies a repository has installed, as its lockfiles record them."""

import json
import sqlite3
from pathlib import Path

from conftest import git

from what import search
from what.dependencies import DependencyReach
from what.index import Index
from what.symbols import Kind

TABLE = """<?php
namespace Phinx\\Db;

class Table {
    public function addIndex($columns, array $options = []) {
        return 'version {version}';
    }
}
"""


def install_composer(
    root: Path,
    packages: dict[str, str],
    *,
    direct: list[str],
    installed: dict[str, str] | None = None,
) -> None:
    """
    Write composer.json, composer.lock and vendor/ for packages, given as {name: version}. Every package is
    installed at its locked version unless installed says otherwise.
    """
    installed = packages if installed is None else installed
    (root / "composer.json").write_text(json.dumps({"require": {name: "*" for name in direct}}))
    (root / "composer.lock").write_text(
        json.dumps({"packages": [{"name": name, "version": version} for name, version in packages.items()]})
    )
    (root / "vendor/composer").mkdir(parents=True, exist_ok=True)
    (root / "vendor/composer/installed.json").write_text(
        json.dumps({"packages": [{"name": name, "version": version} for name, version in installed.items()]})
    )

    for name, version in installed.items():
        folder = root / "vendor" / name / "src/Phinx/Db"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "Table.php").write_text(TABLE.replace("{version}", version).replace("Table", _class(name)))

    (root / ".gitignore").write_text("vendor/\n")


def _class(package: str) -> str:
    """The class a test package defines: "Table" for robmorgan/phinx and "Helper" for any other."""
    return "Table" if package == "robmorgan/phinx" else "Helper"


def test_find_returns_a_class_that_is_only_in_vendor_with_its_package(index: Index, repo: Path) -> None:
    install_composer(repo, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])

    assert search.find(index, "Phinx\\Db\\Table.addIndex").text.splitlines() == [
        "vendor: robmorgan/phinx@0.16.1 src/Phinx/Db/Table.php:5:public function addIndex($columns, array $options = [])",
    ]


def test_find_for_a_class_in_the_repository_does_not_search_dependencies(index: Index, repo: Path) -> None:
    install_composer(repo, {"acme/bender": "1.0.0"}, direct=["acme/bender"])
    (repo / "vendor/acme/bender/src/Phinx/Db/Helper.php").write_text("<?php\nclass BananaBender {}\n")

    assert "vendor:" not in search.find(index, "BananaBender").text
    assert "vendor: acme/bender@1.0.0" in search.find(index, "BananaBender", deps=DependencyReach.DIRECT).text


def test_grep_leaves_out_dependencies_unless_asked(index: Index, repo: Path) -> None:
    install_composer(repo, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])
    (repo / "node_modules/left-pad").mkdir(parents=True)
    (repo / "node_modules/left-pad/index.js").write_text("// version 0.16.1\n")
    (repo / ".venv/lib/python3.12/site-packages").mkdir(parents=True)
    (repo / ".venv/lib/python3.12/site-packages/pad.py").write_text("# version 0.16.1\n")
    (repo / ".gitignore").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Commit the installed dependencies")

    assert search.grep(index, "version 0.16.1").text.startswith("No text matches")
    assert (
        "vendor: robmorgan/phinx@0.16.1 src/Phinx/Db/Table.php:6:        return 'version 0.16.1';"
        in search.grep(index, "version 0.16.1", deps=DependencyReach.DIRECT).text
    )


def test_show_reads_the_version_a_changed_lockfile_installs(index: Index, repo: Path) -> None:
    install_composer(repo, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])
    assert "return 'version 0.16.1';" in search.show(index, "Table.addIndex").text

    install_composer(repo, {"robmorgan/phinx": "0.17.0"}, direct=["robmorgan/phinx"])
    output = search.show(index, "Table.addIndex").text

    assert output.startswith("vendor: robmorgan/phinx@0.17.0 src/Phinx/Db/Table.php:5-7")
    assert "return 'version 0.17.0';" in output


def test_two_repositories_with_the_same_version_share_one_index_entry(
    index: Index,
    repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    install_composer(repo, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])
    install_composer(other, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])

    search.find(index, "addIndex")
    search.find(Index(other, cache_dir=index.cache_dir), "addIndex")

    with sqlite3.connect(index.cache_dir / "dependencies.db") as db:
        assert db.execute("SELECT name, version FROM packages").fetchall() == [("robmorgan/phinx", "0.16.1")]


def test_a_transitive_dependency_is_found_only_with_all_deps(index: Index, repo: Path) -> None:
    install_composer(repo, {"robmorgan/phinx": "0.16.1", "symfony/helper": "6.0.0"}, direct=["robmorgan/phinx"])

    assert search.find(index, "Helper", kind=Kind.CLASS).text.startswith("No class named 'Helper'")
    assert search.find(index, "Helper", kind=Kind.CLASS, deps=DependencyReach.ALL).text.splitlines() == [
        "vendor: symfony/helper@6.0.0 src/Phinx/Db/Table.php:4:class Helper",
    ]


def test_a_package_that_is_not_installed_is_skipped_with_a_note(index: Index, repo: Path) -> None:
    install_composer(
        repo,
        {"robmorgan/phinx": "0.16.1", "acme/peel": "2.0.0"},
        direct=["robmorgan/phinx", "acme/peel"],
        installed={"robmorgan/phinx": "0.16.1"},
    )

    assert search.find(index, "addIndex").text.splitlines() == [
        "vendor: robmorgan/phinx@0.16.1 src/Phinx/Db/Table.php:5:public function addIndex($columns, array $options = [])",
        "1 package in composer.lock isn't installed; run composer install",
    ]


def test_npm_and_python_packages_are_read_from_their_lockfiles(index: Index, repo: Path) -> None:
    (repo / "package.json").write_text("{}")
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"left-pad": "^1.3.0"}},
                    "node_modules/left-pad": {"version": "1.3.0"},
                },
            }
        )
    )
    (repo / "node_modules/left-pad").mkdir(parents=True)
    (repo / "node_modules/left-pad/package.json").write_text('{"version": "1.3.0"}')
    (repo / "node_modules/left-pad/index.js").write_text("function leftPad(text, length) {\n  return text;\n}\n")

    site = repo / ".venv/lib/python3.12/site-packages"
    (site / "peeler").mkdir(parents=True)
    (site / "peeler/__init__.py").write_text("def peel_carefully():\n    pass\n")
    (site / "peeler-2.0.dist-info").mkdir()
    (site / "peeler-2.0.dist-info/RECORD").write_text("peeler/__init__.py,sha256=x,10\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "fruit"\ndependencies = ["Peeler>=2"]\n')
    (repo / "uv.lock").write_text(
        '[[package]]\nname = "fruit"\nversion = "0.1.0"\nsource = { editable = "." }\n\n'
        '[[package]]\nname = "peeler"\nversion = "2.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    )
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n")

    assert search.find(index, "leftPad").text.splitlines() == [
        "node_modules: left-pad@1.3.0 index.js:1:function leftPad(text, length)",
    ]
    assert search.find(index, "peel_carefully").text.splitlines() == [
        "site-packages: peeler@2.0 peeler/__init__.py:1:def peel_carefully():",
    ]


def test_a_composer_package_installed_outside_vendor_is_found_where_installed_json_says(
    index: Index,
    repo: Path,
) -> None:
    install_composer(repo, {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])
    (repo / "vendor/robmorgan/phinx").rename(repo / "phinx")
    record = json.loads((repo / "vendor/composer/installed.json").read_text())
    record["packages"][0]["install-path"] = "../../phinx"
    (repo / "vendor/composer/installed.json").write_text(json.dumps(record))
    (repo / ".gitignore").write_text("vendor/\nphinx/\n")

    assert search.find(index, "addIndex").text.splitlines() == [
        "vendor: robmorgan/phinx@0.16.1 src/Phinx/Db/Table.php:5:public function addIndex($columns, array $options = [])",
    ]


def test_a_dev_branch_package_is_indexed_again_when_its_commit_changes(index: Index, repo: Path) -> None:
    install_composer(repo, {"robmorgan/phinx": "dev-main"}, direct=["robmorgan/phinx"])
    installed = repo / "vendor/composer/installed.json"
    record = json.loads(installed.read_text())
    record["packages"][0]["source"] = {"reference": "aaa111"}
    installed.write_text(json.dumps(record))
    search.find(index, "addIndex")

    record["packages"][0]["source"] = {"reference": "bbb222"}
    installed.write_text(json.dumps(record))
    table = repo / "vendor/robmorgan/phinx/src/Phinx/Db/Table.php"
    table.write_text(table.read_text().replace("addIndex", "addKey"))

    assert search.find(index, "addKey").text.startswith("vendor: robmorgan/phinx@dev-main src/Phinx/Db/Table.php:5:")


def test_a_package_installed_at_another_version_is_searched_with_a_note(index: Index, repo: Path) -> None:
    install_composer(
        repo, {"robmorgan/phinx": "0.16.12"}, direct=["robmorgan/phinx"], installed={"robmorgan/phinx": "0.16.11"}
    )

    assert search.find(index, "addIndex").text.splitlines() == [
        "vendor: robmorgan/phinx@0.16.11 src/Phinx/Db/Table.php:5:public function addIndex($columns, array $options = [])",
        "1 package in composer.lock is installed at another version, which is what's searched; run composer install",
    ]


def test_an_optional_npm_package_that_is_not_installed_gets_no_note(index: Index, repo: Path) -> None:
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"optionalDependencies": {"fsevents": "^2"}},
                    "node_modules/fsevents": {"version": "2.3.3", "optional": True},
                },
            }
        )
    )

    assert "package-lock.json" not in search.find(index, "nothing_is_called_this").text


def test_a_lockfile_in_a_subfolder_is_read_and_its_vendor_left_out_of_grep(index: Index, repo: Path) -> None:
    (repo / "include").mkdir()
    install_composer(repo / "include", {"robmorgan/phinx": "0.16.1"}, direct=["robmorgan/phinx"])
    (repo / "include/.gitignore").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Commit a PHP project in a subfolder, vendor included")

    assert search.find(index, "addIndex").text.splitlines() == [
        "vendor: robmorgan/phinx@0.16.1 src/Phinx/Db/Table.php:5:public function addIndex($columns, array $options = [])",
    ]
    assert search.grep(index, "version 0.16.1").text.startswith("No text matches")
