"""
The installed dependencies of a repository, read from its lockfiles, and the index of their symbols that every
repository shares. A package version is the same in every project, so each one is indexed once.

The lockfiles read are composer.lock, package-lock.json, uv.lock and poetry.lock. A package counts only when the
version installed is the version the lockfile records.
"""

import enum
import fnmatch
import json
import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from what import models
from what.config import settings
from what.index import Index
from what.models import Place, SymbolHit
from what.symbols import extract


class DependencyReach(enum.Enum):
    """When a search reads the installed dependencies, and which ones it reads."""

    # Only when the repository has no match, and only direct dependencies. grep never reads them this way.
    FALLBACK = enum.auto()

    # On every search, direct dependencies only.
    DIRECT = enum.auto()

    # On every search, with the dependencies of dependencies too.
    ALL = enum.auto()


@dataclass(frozen=True)
class Package:
    """
    One installed dependency. folder holds its files. files lists the paths of its source files, relative to
    folder, when the package manager records them. Otherwise folder is walked. For example:

        Package("composer", "robmorgan/phinx", "0.16.1", "", "vendor", Path("/repo/vendor/robmorgan/phinx"), None, True)
    """

    ecosystem: str
    name: str
    version: str
    # What pins the content beyond the version, such as the commit of a Composer "dev-main" package. Empty when the
    # version alone does.
    pin: str
    # Where the package manager installs, as the start of each hit's label: "vendor", "node_modules" or
    # "site-packages".
    location: str
    folder: Path
    files: tuple[str, ...] | None
    direct: bool

    @property
    def cache_version(self) -> str:
        """The version the shared index keys the package on, with its pin when it has one, such as "dev-main#a1b2c3"."""
        return f"{self.version}#{self.pin}" if self.pin else self.version

    @property
    def label(self) -> str:
        """How output names the package, such as "vendor: robmorgan/phinx@0.16.1"."""
        return f"{self.location}: {self.name}@{self.version}"


@dataclass(frozen=True)
class Installed:
    """
    The dependencies a repository has installed, and a note for each lockfile with packages that aren't installed,
    such as "3 packages in composer.lock aren't installed; run composer install".
    """

    packages: list[Package]
    notes: list[str]


def installed(root: Path, transitive: bool) -> Installed:
    """
    The dependencies installed at root, as its lockfiles record them. Only direct dependencies are litsed unless
    transitive is True.
    """
    packages: list[Package] = []
    notes: list[str] = []

    for lockfile, command, read in [
        ("composer.lock", "composer install", _composer),
        ("package-lock.json", "npm install", _npm),
        ("uv.lock", "uv sync", _python),
        ("poetry.lock", "poetry install", _python),
    ]:
        if not (root / lockfile).is_file():
            continue

        found, missing = read(root, lockfile)
        packages.extend(package for package in found if transitive or package.direct)

        if lockfile == "package-lock.json" and "packages" not in _json(root / lockfile):
            notes.append(
                "package-lock.json is lockfile version 1, which isn't read; run npm install with npm 7 or later"
            )

        missing = [name for name, direct in missing if transitive or direct]
        if missing:
            count = "1 package" if len(missing) == 1 else f"{len(missing)} packages"
            verb = "isn't" if len(missing) == 1 else "aren't"
            notes.append(f"{count} in {lockfile} {verb} installed; run {command}")

    return Installed(packages, notes)


def dependency_folders(root: Path) -> list[str]:
    """
    The folders, relative to root, that hold installed dependencies rather than the repository's own code, such as
    "vendor/", "node_modules/" and ".venv/".
    """
    folders = ["vendor/", "node_modules/"]
    site = _site_packages(root)
    if site is not None and site.is_relative_to(root):
        folders.append(site.relative_to(root).parts[0] + "/")

    return folders


def package_rows(index: Index, packages: list[Package]) -> dict[int, Package]:
    """
    The row of each package in the shared index, indexing the ones it doesn't have yet. A package is parsed once
    for every repository that uses the same version.
    """
    rows: dict[int, Package] = {}

    with models.connect_dependencies(_index_path(index)) as session:
        for package in packages:
            row = session.package_row(package.ecosystem, package.name, package.cache_version)
            if row is None:
                files = {path: extract(path, text) for path, text in _source_files(index, package)}
                with session.write_transaction():
                    # Another process may have indexed the package while this one read it.
                    row = session.package_row(package.ecosystem, package.name, package.cache_version)
                    if row is None:
                        row = session.insert_package(package.ecosystem, package.name, package.cache_version, files)
            rows[row] = package

    return rows


def find_symbols(
    index: Index,
    packages: dict[int, Package],
    name: str,
    words: list[str],
    parent: str | None,
    kind: str | None,
) -> list[list[SymbolHit]]:
    """Symbols in the packages matching a name, as three tiers: exact, then prefix, then containing every word."""
    rows = list(packages)

    with models.connect_dependencies(_index_path(index)) as session:
        tiers = [
            session.symbols_named(name, parent, kind, rows),
            session.symbols_starting_with(name, parent, kind, rows),
            session.symbols_containing_all(words, parent, kind, rows),
        ]

    return [[_hit(packages[symbol.package], symbol) for symbol in tier] for tier in tiers]


def source_places(index: Index, packages: dict[int, Package]) -> list[Place]:
    """Every source file of the packages, for a text search to read from disk."""
    with models.connect_dependencies(_index_path(index)) as session:
        files = session.package_files(list(packages))

    return [_place(packages[row], path) for row, path in files]


def _hit(package: Package, symbol: models.PackageSymbol) -> SymbolHit:
    place = _place(package, symbol.path)
    return SymbolHit(place, symbol.name, symbol.kind, symbol.parent, symbol.line, symbol.end_line, symbol.signature)


def _place(package: Package, path: str) -> Place:
    return Place(path, "", 0, package=package.label, package_folder=str(package.folder))


def _index_path(index: Index) -> Path:
    return index.cache_dir / "dependencies.db"


def _source_files(index: Index, package: Package) -> Iterator[tuple[str, str]]:
    """Each source file of a package that can be parsed, as (path relative to its folder, text)."""
    paths = package.files if package.files is not None else _walk(package.folder)

    for path in paths:
        name = path.rsplit("/", 1)[-1]
        skipped = any(fnmatch.fnmatch(name, pattern) for pattern in settings.SKIPPED_NAMES)
        if skipped or not name.endswith(settings.DEPENDENCY_SUFFIXES):
            continue

        text = index.read_text(package.folder / path)
        if text is not None:
            yield path, text


def _walk(folder: Path) -> list[str]:
    """Every file under a package's folder. A nested node_modules holds other packages, so it is left out."""
    found: list[str] = []

    for directory, subdirectories, names in os.walk(folder):
        subdirectories[:] = [name for name in subdirectories if name != "node_modules" and not name.startswith(".")]
        relative = os.path.relpath(directory, folder)
        found.extend(name if relative == "." else f"{relative}/{name}" for name in names)

    return sorted(found)


def _composer(root: Path, lockfile: str) -> tuple[list[Package], list[tuple[str, bool]]]:
    """
    The packages in composer.lock that are installed in vendor/, and the (name, direct) of those that aren't. The
    direct dependencies are the ones composer.json requires.
    """
    lock = _json(root / lockfile)
    manifest = _json(root / "composer.json")
    direct = {*manifest.get("require", {}), *manifest.get("require-dev", {})}

    # Composer 2 writes {"packages": [...]} to installed.json, and Composer 1 writes the list on its own.
    record = _json(root / "vendor/composer/installed.json")
    listed = cast(list[dict[str, str]], record if isinstance(record, list) else record.get("packages", []))
    installs = {entry["name"]: entry for entry in listed}

    found: list[Package] = []
    missing: list[tuple[str, bool]] = []
    for entry in [*lock.get("packages", []), *lock.get("packages-dev", [])]:
        name, version = entry["name"], entry["version"]
        if entry.get("type") == "metapackage" or entry.get("dist", {}).get("type") == "path":
            # A metapackage has no files. A path package is the project's own code, which can change under one version.
            continue

        install = installs.get(name, {})
        # Custom installers put some packages outside vendor/, such as Drupal modules. installed.json says where,
        # relative to vendor/composer.
        folder = (
            (root / "vendor/composer" / install["install-path"])
            if "install-path" in install
            else root / "vendor" / name
        )
        if install.get("version") != version or not folder.is_dir():
            missing.append((name, name in direct))
            continue

        pin = entry.get("source", {}).get("reference") or entry.get("dist", {}).get("reference") or ""
        found.append(Package("composer", name, version, pin, "vendor", folder.resolve(), None, name in direct))

    return found, missing


def _npm(root: Path, lockfile: str) -> tuple[list[Package], list[tuple[str, bool]]]:
    """
    The packages in package-lock.json that are installed in node_modules, and the (name, direct) of those that
    aren't. Each key of "packages" is the folder the package is installed in, so nested installs are found too.
    """
    lock = _json(root / lockfile)
    packages: dict[str, Any] = lock.get("packages", {})
    top = packages.get("", {})
    direct = {*top.get("dependencies", {}), *top.get("devDependencies", {}), *top.get("optionalDependencies", {})}

    found: list[Package] = []
    missing: list[tuple[str, bool]] = []
    for key, entry in packages.items():
        if not key.startswith("node_modules/") or entry.get("link") or "version" not in entry:
            continue

        name = key.rsplit("node_modules/", 1)[-1]
        is_direct = key == f"node_modules/{name}" and name in direct
        folder = root / key
        if _json(folder / "package.json").get("version") != entry["version"]:
            # An optional package is often for another platform, such as fsevents on Linux, so its absence is normal.
            if not entry.get("optional"):
                missing.append((name, is_direct))
            continue

        pin = entry.get("integrity") or entry.get("resolved") or ""
        found.append(Package("npm", name, entry["version"], pin, "node_modules", folder, None, is_direct))

    return found, missing


def _python(root: Path, lockfile: str) -> tuple[list[Package], list[tuple[str, bool]]]:
    """
    The packages in uv.lock or poetry.lock that are installed in the virtualenv, and the (name, direct) of those
    that arent. The direct dependencies are the ones pyproject.toml names. The files of each package come from the
    RECORD file pip and uv write, since a distribution's name is often not the name of its folder.
    """
    with open(root / lockfile, "rb") as file:
        lock = tomllib.load(file)
    direct = _python_direct(root / "pyproject.toml")
    site = _site_packages(root)
    installs = _distributions(site) if site is not None else {}

    entries = lock.get("package", [])
    # uv.lock can list several versions of one package, one for each platform, and only one gets installed.
    installed_names = {
        _normal_name(entry["name"])
        for entry in entries
        if (_normal_name(entry["name"]), entry.get("version")) in installs
    }

    found: list[Package] = []
    missing: list[tuple[str, bool]] = []
    for entry in entries:
        name, version = entry["name"], entry.get("version")
        source = entry.get("source", {})
        if version is None or {"editable", "virtual", "path", "directory"} & set(source):
            # The project itself, or local code that can change under one version.
            continue

        is_direct = _normal_name(name) in direct
        record = installs.get((_normal_name(name), version))
        if site is None or record is None:
            if _normal_name(name) not in installed_names:
                missing.append((name, is_direct))
            continue

        # A package from git can change under one version, so its commit is part of the key.
        pin = source.get("git", "")
        found.append(Package("python", name, version, pin, "site-packages", site, _record_files(record), is_direct))

    return found, missing


def _python_direct(pyproject: Path) -> set[str]:
    """The names of the dependencies pyproject.toml lists itself, in the PEP 621, dependency group and Poetry forms."""
    try:
        with open(pyproject, "rb") as file:
            project = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return set()

    requirements: list[str] = [*project.get("project", {}).get("dependencies", [])]
    for group in project.get("project", {}).get("optional-dependencies", {}).values():
        requirements.extend(group)
    for group in project.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))

    poetry = project.get("tool", {}).get("poetry", {})
    names = {*poetry.get("dependencies", {}), *poetry.get("dev-dependencies", {})}
    for group in poetry.get("group", {}).values():
        names.update(group.get("dependencies", {}))

    # A requirement starts with the name, as in "requests>=2.31" or "uvicorn[standard]".
    for requirement in requirements:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if match:
            names.add(match.group(0))

    return {_normal_name(name) for name in names if name != "python"}


def _site_packages(root: Path) -> Path | None:
    """The site-packages folder of the repository's virtualenv, or None when it has none."""
    for venv in [root / ".venv", root / "venv", Path(os.environ.get("VIRTUAL_ENV", root / ".venv"))]:
        for site in sorted(venv.glob("lib/python*/site-packages")):
            return site

    return None


def _distributions(site: Path) -> dict[tuple[str, str], Path]:
    """The RECORD filke of every installed distribution, by (normalised name, version)."""
    found: dict[tuple[str, str], Path] = {}

    for info in site.glob("*.dist-info"):
        name, _, version = info.name.removesuffix(".dist-info").partition("-")
        found[(_normal_name(name), version)] = info / "RECORD"

    return found


def _record_files(record: Path) -> tuple[str, ...]:
    """
    The paths a RECORD file lists, relative to site-packages. Each line is "path,hash,size". Paths outside
    site-packages, such as scripts, start with "../" and are left out.
    """
    try:
        lines = record.read_text(errors="replace").splitlines()
    except OSError:
        return ()

    paths = (line.split(",", 1)[0] for line in lines)
    return tuple(path for path in paths if path and not path.startswith("../"))


def _normal_name(name: str) -> str:
    """A Python distribution name as PEP 503 compares it: "Typing_Extensions" and "typing-extensions" are the same."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _json(path: Path) -> Any:
    """The JSON in a file, or an empty object when it is missing or broken."""
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}
