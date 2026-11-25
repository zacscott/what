"""
The tables of the index, one open connection to them, and the SQL that reads and writes them. No other
module holds SQL.

A row in blobs is one file content, keyed on git's blob id. The files table
says which content each path holds now. The deleted table says which content
a deleted path held last. The branch_files table says which content each other branch
holds where it differs from its base. The symbols and text_fts tables hold what each blob's text was indexed as.
"""

import enum
import fnmatch
import re
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from what.symbols import Symbol

# Changing the tables means raising this. An index with another version is rebuilt from nothing.
SCHEMA_VERSION = 4

# How many rows one tier of a symbol search fetches before ranking.
TIER_FETCH = 300

SCHEMA = """
CREATE TABLE blobs (
    id INTEGER PRIMARY KEY,
    oid TEXT NOT NULL,
    parsed_as TEXT NOT NULL,
    UNIQUE (oid, parsed_as)
);
CREATE TABLE symbols (
    blob INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,
    parent TEXT COLLATE NOCASE,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL
);
CREATE INDEX symbols_name ON symbols (name);
CREATE INDEX symbols_blob ON symbols (blob);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    blob INTEGER NOT NULL,
    stat TEXT
);
CREATE INDEX files_blob ON files (blob);
CREATE TABLE deleted (
    path TEXT PRIMARY KEY,
    blob INTEGER NOT NULL,
    commit_id TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE INDEX deleted_blob ON deleted (blob);
-- The branches whose files are indexed. tip is the commit the branch pointed at, and head is the commit HEAD pointed
-- at, when its files were listed. date is the time of the tip commit, in seconds since 1970.
CREATE TABLE branches (
    name TEXT PRIMARY KEY,
    tip TEXT NOT NULL,
    head TEXT NOT NULL,
    date INTEGER NOT NULL
);
CREATE TABLE branch_files (
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    blob INTEGER NOT NULL,
    PRIMARY KEY (branch, path)
);
CREATE INDEX branch_files_blob ON branch_files (blob);
-- Names of symbols in a trigram index, so a search for part of a name doesn't read every row of symbols. It
-- reads each name from its symbols row, which a trigram LIKE needs to check a match.
CREATE VIRTUAL TABLE symbol_names USING fts5(
    name, content='symbols', content_rowid='rowid', tokenize='trigram case_sensitive 0'
);
CREATE VIRTUAL TABLE text_fts USING fts5(
    body, content='', contentless_delete=1, tokenize='trigram case_sensitive 0'
);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Changing the dependency tables means raising this. A dependency index with another version is rebuilt from nothing.
DEPENDENCY_SCHEMA_VERSION = 1

# The index of installed dependencies, which every repository shares. A row in packages is one version of one
# package, and is written only once its files and symbols are all in. Paths are relative to the package's folder.
DEPENDENCY_SCHEMA = """
CREATE TABLE packages (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    UNIQUE (ecosystem, name, version)
);
CREATE TABLE package_files (
    package INTEGER NOT NULL,
    path TEXT NOT NULL
);
CREATE INDEX package_files_package ON package_files (package);
CREATE TABLE package_symbols (
    package INTEGER NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,
    parent TEXT COLLATE NOCASE,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL
);
CREATE INDEX package_symbols_name ON package_symbols (name);
CREATE INDEX package_symbols_package ON package_symbols (package);
"""


class Scope(enum.Enum):
    """Which files a search reads."""

    # The files in the working tree, and the files that HEAD's history deleted.
    WORKING_TREE = enum.auto()

    # The files on other branches whose content differs from the working tree's file at the same path.
    BRANCHES = enum.auto()


@dataclass(frozen=True)
class Place:
    """
    Where a file's content comes from. commit is set only for a deleted file. tree is set only for a file read at a
    git ref, and holds the ref as the user gave it, such as "origin/dev". branch is set only for a file on another
    branch, with branch_date the time of the branch's last commit. package is set only for a file in an installed
    dependency, with the package's label, such as "vendor: robmorgan/phinx@0.16.1". Its path is then relative to
    package_folder.
    """

    path: str
    oid: str
    blob_row: int
    commit: str | None = None
    tree: str | None = None
    branch: str | None = None
    branch_date: int | None = None
    package: str | None = None
    package_folder: str | None = None

    @property
    def ref(self) -> str:
        """
        How output names the file. A deleted file is named the way git names a file at a commit, in the commit
        before the one that deleted it, such as "a1b2c3d4^:legacy/split.py". A file at a ref is named
        "origin/dev:src/a.py".
        """
        if self.package is not None:
            return f"{self.package} {self.path}"
        if self.branch is not None:
            return f"{self.branch} ({age(self.branch_date or 0)}) {self.path}"
        if self.tree is not None:
            return f"{self.tree}:{self.path}"
        if self.commit is not None:
            return f"{self.commit[:8]}^:{self.path}"

        return self.path

    @property
    def in_working_tree(self) -> bool:
        """True when the file is read from disk, and False when it is read from git."""
        return self.commit is None and self.tree is None and self.branch is None and self.package is None


@dataclass(frozen=True)
class SymbolHit:
    """
    One symbol a search found, and the file it is in. The searches take the same
    filters. parent and kind must match exactly, and path works as path_matches does.
    """

    place: Place
    name: str
    kind: str
    parent: str | None
    line: int
    end_line: int
    signature: str


class _Session:
    """One open connection to an SQLite file, which creates the tables when the file is new or has an old schema."""

    def __init__(
        self,
        db: sqlite3.Connection,
        schema: str,
        version: int,
    ) -> None:
        self._db = db
        self._ensure_schema(schema, version)

    @contextmanager
    def write_transaction(self) -> Generator[None]:
        """
        Run the block in one transaction. The transaction takes the write lock at its
        start, so no other process can write between this one's reads and writes.
        """
        self._db.execute("BEGIN IMMEDIATE")
        yield
        self._db.execute("COMMIT")

    def _ensure_schema(self, schema: str, version: int) -> None:
        """Create the tables when the file is new or has an old schema  version."""
        if self._schema_version() == version:
            return

        with self.write_transaction():
            # Another process may have created the tables while this one waited for the lock.
            if self._schema_version() == version:
                return

            # The names come from the schema, so a table added to it is dropped too when the version changes.
            for name in re.findall(r"CREATE (?:VIRTUAL )?TABLE (\w+)", schema):
                self._db.execute(f"DROP TABLE IF EXISTS {name}")

            for statement in schema.split(";"):
                if statement.strip():
                    self._db.execute(statement)

            self._db.execute(f"PRAGMA user_version = {version}")

    def _schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]


class IndexSession(_Session):
    """One open connection to the index of one repository, with the queries that read and write it."""

    def __init__(self, db: sqlite3.Connection) -> None:
        super().__init__(db, SCHEMA, SCHEMA_VERSION)

    def places_at_path(self, path: str) -> list[Place]:
        """The file at exactly this path, as a list of one, or empty when there is none."""
        return self._places_where("path = ?", [path], Scope.WORKING_TREE)

    def places_ending_with(self, path: str) -> list[Place]:
        """Every file whose path ends with this one after a slash, so "a.py" matches "src/a.py"."""
        return self._places_where("path LIKE ? ESCAPE '\\'", ["%/" + _like_escape(path)], Scope.WORKING_TREE)

    def places_containing_all(self, words: list[str], scope: Scope = Scope.WORKING_TREE) -> list[Place]:
        """Files whose path contains every word."""
        condition = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in words)
        return self._places_where(condition, ["%" + _like_escape(word) + "%" for word in words], scope)

    def places_containing_in_order(self, parts: list[str]) -> list[Place]:
        """Files whose payh contains every part, in order, with anything between."""
        return self._places_where("path LIKE ? ESCAPE '\\'", [_in_order(parts)], Scope.WORKING_TREE)

    def live_places_with_text(self, literals: list[str]) -> list[Place]:
        """
        Live files whose text contains every literal, best match first. With no
        literals, every live file is a candidate.
        """
        return self._places_with_text(literals, "files")

    def deleted_places_with_text(self, literals: list[str]) -> list[Place]:
        """
        Deleted files whose text contains every literal, best match first. With no
        literals, every deleted file is a candidate.
        """
        return self._places_with_text(literals, "deleted")

    def branch_places_with_text(self, literals: list[str]) -> list[Place]:
        """
        Files on other branches whose text contains every literal, best match first. A content that several branches
        hold is listed once, for the branch with the newest commit. With no literals, every branch file is a candidate.
        """
        not_live = "NOT EXISTS (SELECT 1 FROM files f WHERE f.path = t.path AND f.blob = t.blob)"
        columns = "t.path, b.oid, b.id, t.branch, br.date"
        joins = "JOIN blobs b ON b.id = t.blob JOIN branches br ON br.name = t.branch"

        if not literals:
            sql = f"SELECT {columns} FROM branch_files t {joins} WHERE {not_live} ORDER BY t.path, br.date DESC"
            rows = self._db.execute(sql)
        else:
            match = " AND ".join('"' + literal.replace('"', '""') + '"' for literal in literals)
            sql = f"""
                SELECT {columns} FROM text_fts JOIN branch_files t ON t.blob = text_fts.rowid {joins}
                WHERE text_fts MATCH ? AND {not_live}
                ORDER BY bm25(text_fts), t.path, br.date DESC
            """
            rows = self._db.execute(sql, (match,))

        return _newest_per_content(
            [Place(path, oid, blob, branch=branch, branch_date=date) for path, oid, blob, branch, date in rows]
        )

    def symbols_named(
        self,
        name: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
        scope: Scope = Scope.WORKING_TREE,
    ) -> list[SymbolHit]:
        """Symbols with exactly this name, ignoring case."""
        return self._matching_symbols("s.name = ?", [name], parent, kind, path, scope)

    def symbols_starting_with(
        self,
        prefix: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
        scope: Scope = Scope.WORKING_TREE,
    ) -> list[SymbolHit]:
        """Symbols whose name starts with this text."""
        return self._matching_symbols(
            "s.name LIKE ? ESCAPE '\\'", [_like_escape(prefix) + "%"], parent, kind, path, scope
        )

    def symbols_containing_all(
        self,
        words: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
        scope: Scope = Scope.WORKING_TREE,
    ) -> list[SymbolHit]:
        """Symbols whose name contains every word."""
        conditions = ["s.name LIKE ? ESCAPE '\\'" for _ in words]
        parameters = ["%" + _like_escape(word) + "%" for word in words]

        # The trigram index naerows the search by the longest word, which matches the fewest names. A word of 3
        # characters or fewer is in so many names that reading rows until the limit is faster than looking it up. The
        # index treats _ as any character, so the LIKE on s.name still decides the match.
        longest = max(words, key=len)
        if len(longest) >= 4:
            conditions.append("s.rowid IN (SELECT rowid FROM symbol_names WHERE name LIKE ?)")
            parameters.append("%" + longest.replace("%", "") + "%")

        return self._matching_symbols(" AND ".join(conditions), parameters, parent, kind, path, scope)

    def symbols_containing_in_order(
        self,
        parts: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list[SymbolHit]:
        """Symbols whose name contains every part, in order, with anything between."""
        return self._matching_symbols(
            "s.name LIKE ? ESCAPE '\\'", [_in_order(parts)], parent, kind, path, Scope.WORKING_TREE
        )

    def symbols_in_blob(self, blob: int) -> list[SymbolHit]:
        """
        Every symbol in one file's content, in line order, outer symbols first.
        Their place holds only the blob row.
        """
        rows = self._db.execute(
            "SELECT name, kind, parent, line, end_line, signature FROM symbols WHERE blob = ? ORDER BY line, end_line DESC",
            (blob,),
        )

        place = Place("", "", blob)
        return [SymbolHit(place, *row) for row in rows]

    def blob_row(self, oid: str, parsed_as: str) -> int | None:
        """The row id of a content parsed this way, or None when the index hasn't seen it."""
        row = self._db.execute("SELECT id FROM blobs WHERE oid = ? AND parsed_as = ?", (oid, parsed_as)).fetchone()
        return row[0] if row else None

    def insert_blob(
        self,
        oid: str,
        parsed_as: str,
        text: str | None,
        symbols: list[Symbol],
    ) -> int:
        """Store a new content and return its row id. When text is None, only the row is stored."""
        insert = "INSERT INTO blobs (oid, parsed_as) VALUES (?, ?) RETURNING id"
        blob: int = self._db.execute(insert, (oid, parsed_as)).fetchone()[0]

        if text is not None:
            self._db.execute("INSERT INTO text_fts (rowid, body) VALUES (?, ?)", (blob, text))
            self._db.executemany(
                "INSERT INTO symbols (blob, name, kind, parent, line, end_line, signature) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(blob, s.name, s.kind, s.parent, s.line, s.end_line, s.signature) for s in symbols],
            )
            self._db.execute(
                "INSERT INTO symbol_names (rowid, name) SELECT rowid, name FROM symbols WHERE blob = ?", (blob,)
            )

        return blob

    def insert_symbol_name(self, value: str) -> None:
        """
        Put one value into the symbol name index. The index is content-backed by the symbols table, so this is
        how a value that the symbols table does not hold gets in. Not for normal use.
        """
        self._db.execute("INSERT INTO symbol_names (symbol_names) VALUES (?)", (value,))

    def delete_unused_blobs(self) -> None:
        """Delete every content that no live or deleted file holds, wiht its text and symbols."""
        unused = (
            "SELECT id FROM blobs WHERE id NOT IN (SELECT blob FROM files) AND id NOT IN (SELECT blob FROM deleted) "
            "AND id NOT IN (SELECT blob FROM branch_files)"
        )

        with self.write_transaction():
            # An index that reads its text from symbols must be told what each row held before the row goes.
            self._db.execute(
                "INSERT INTO symbol_names (symbol_names, rowid, name) "
                f"SELECT 'delete', rowid, name FROM symbols WHERE blob IN ({unused})"
            )
            self._db.execute(f"DELETE FROM symbols WHERE blob IN ({unused})")
            self._db.execute(f"DELETE FROM text_fts WHERE rowid IN ({unused})")
            self._db.execute(f"DELETE FROM blobs WHERE id IN ({unused})")

    def delete_meta(self, key: str) -> None:
        """Forget a value the index keeps about itself."""
        self._db.execute("DELETE FROM meta WHERE key = ?", (key,))

    def known_files(self) -> dict[str, tuple[str, str | None]]:
        """Every path in the file table, with its blob id and stat."""
        rows = self._db.execute("SELECT f.path, b.oid, f.stat FROM files f JOIN blobs b ON b.id = f.blob")
        return {path: (oid, stat) for path, oid, stat in rows}

    def save_file(self, path: str, blob: int, stat: str | None) -> None:
        """Record the content a path holds, replacing what it held before."""
        self._db.execute("INSERT OR REPLACE INTO files (path, blob, stat) VALUES (?, ?, ?)", (path, blob, stat))

    def delete_files(self, paths: list[str]) -> None:
        """Forget these paths. Their blobs stay until delete_unused_blobs runs."""
        self._db.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in paths])

    def save_deleted_file(self, path: str, blob: int, commit: str, date: str) -> None:
        """Record the content a deleted path held and the commit that deleted it, replacing ayn earlier record."""
        self._db.execute(
            "INSERT OR REPLACE INTO deleted (path, blob, commit_id, date) VALUES (?, ?, ?, ?)",
            (path, blob, commit, date),
        )

    def delete_all_deleted_files(self) -> None:
        """Forget every deleted path, so the next history build starts again."""
        self._db.execute("DELETE FROM deleted")

    def known_branches(self) -> dict[str, tuple[str, str]]:
        """Every indexed branch, with the commits its tip and HEAD pointed at when its files were listed."""
        return {name: (tip, head) for name, tip, head in self._db.execute("SELECT name, tip, head FROM branches")}

    def save_branch_files(self, name: str, files: list[tuple[str, int]]) -> None:
        """Record the contetn of files a branch changed, as (path, blob row)."""
        self._db.executemany(
            "INSERT OR REPLACE INTO branch_files (branch, path, blob) VALUES (?, ?, ?)",
            [(name, path, blob) for path, blob in files],
        )

    def save_branch(
        self,
        name: str,
        tip: str,
        head: str,
        date: int,
    ) -> None:
        """Record that a branch's files were listed when it pointed at tip and HEAD pointed at head."""
        self._db.execute(
            "INSERT OR REPLACE INTO branches (name, tip, head, date) VALUES (?, ?, ?, ?)", (name, tip, head, date)
        )

    def delete_branches(self, names: list[str]) -> None:
        """Forget these branches and their files. Their blobs stay until delete_unused_blobs runs."""
        self._db.executemany("DELETE FROM branch_files WHERE branch = ?", [(name,) for name in names])
        self._db.executemany("DELETE FROM branches WHERE name = ?", [(name,) for name in names])

    def meta(self, key: str) -> str | None:
        """A value the index keeps about itself, or None when it has never been set."""
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Record a value the index keeps about itself."""
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _places_where(self, condition: str, parameters: list[str], scope: Scope) -> list[Place]:
        """
        Files matching a condition on path. A path that exists now is never also listed as deleted, or as on
        another branch.
        """
        if scope == Scope.BRANCHES:
            sql = f"""
                SELECT t.path, b.oid, b.id, t.branch, br.date
                FROM branch_files t JOIN blobs b ON b.id = t.blob JOIN branches br ON br.name = t.branch
                WHERE {condition} AND t.path NOT IN (SELECT path FROM files)
                ORDER BY br.date DESC
            """
            rows = self._db.execute(sql, parameters)
            return _newest_per_content(
                [Place(path, oid, blob, branch=branch, branch_date=date) for path, oid, blob, branch, date in rows]
            )

        sql = f"""
            SELECT f.path, b.oid, b.id, NULL FROM files f JOIN blobs b ON b.id = f.blob WHERE {condition}
            UNION ALL
            SELECT d.path, b.oid, b.id, d.commit_id FROM deleted d JOIN blobs b ON b.id = d.blob
            WHERE {condition} AND d.path NOT IN (SELECT path FROM files)
        """

        return [Place(*row) for row in self._db.execute(sql, parameters + parameters)]

    def _places_with_text(self, literals: list[str], table: str) -> list[Place]:
        history = "t.commit_id" if table == "deleted" else "NULL"
        still_gone = "AND t.path NOT IN (SELECT path FROM files)" if table == "deleted" else ""

        if not literals:
            sql = f"SELECT t.path, b.oid, b.id, {history} FROM {table} t JOIN blobs b ON b.id = t.blob ORDER BY t.path"
            return [Place(*row) for row in self._db.execute(sql)]

        match = " AND ".join('"' + literal.replace('"', '""') + '"' for literal in literals)
        sql = f"""
            SELECT t.path, b.oid, b.id, {history}
            FROM text_fts JOIN {table} t ON t.blob = text_fts.rowid JOIN blobs b ON b.id = t.blob
            WHERE text_fts MATCH ? {still_gone}
            ORDER BY bm25(text_fts), t.path
        """

        return [Place(*row) for row in self._db.execute(sql, (match,))]

    def _matching_symbols(
        self,
        condition: str,
        parameters: list[str],
        parent: str | None,
        kind: str | None,
        path_filter: str | None,
        scope: Scope,
    ) -> list[SymbolHit]:
        """
        Symbols matching a condition on the symbols table, in files the path filter allows. In the working tree, live
        symbols come before deleted ones. On branches, a content that several branches hold is listed for the branch
        with the newest commit.
        """
        # The filters go into the SQL. Applied after the row limit, they would lose a match
        # among many same-named symbols.
        if parent:
            condition += " AND s.parent = ?"
            parameters = [*parameters, parent]
        if kind:
            condition += " AND s.kind = ?"
            parameters = [*parameters, kind.lower()]

        columns = "s.name, s.kind, s.parent, s.line, s.end_line, s.signature, b.oid, b.id"
        path_condition, path_parameters = _path_sql(path_filter)

        if scope == Scope.BRANCHES:
            sql = f"""
                SELECT * FROM (
                    SELECT t.path AS path, NULL, t.branch, br.date AS date, {columns}
                    FROM symbols s JOIN blobs b ON b.id = s.blob JOIN branch_files t ON t.blob = s.blob
                    JOIN branches br ON br.name = t.branch
                    WHERE {condition} AND NOT EXISTS (SELECT 1 FROM files f WHERE f.path = t.path AND f.blob = t.blob)
                )
                WHERE {path_condition}
                ORDER BY date DESC
                LIMIT {TIER_FETCH}
            """
            parameters = parameters + path_parameters
        else:
            sql = f"""
                SELECT * FROM (
                    SELECT f.path AS path, NULL, NULL, NULL, {columns}
                    FROM symbols s JOIN blobs b ON b.id = s.blob JOIN files f ON f.blob = s.blob
                    WHERE {condition}
                    UNION ALL
                    SELECT d.path AS path, d.commit_id, NULL, NULL, {columns}
                    FROM symbols s JOIN blobs b ON b.id = s.blob JOIN deleted d ON d.blob = s.blob
                    WHERE {condition} AND d.path NOT IN (SELECT path FROM files)
                )
                WHERE {path_condition}
                LIMIT {TIER_FETCH}
            """
            parameters = parameters + parameters + path_parameters

        hits: list[SymbolHit] = []
        seen: set[tuple[str, int, str, int]] = set()
        for row in self._db.execute(sql, parameters):
            file_path, commit, branch, date, *symbol, oid, blob = row
            line = symbol[3]

            # Rows on branches come newest branch first, so the first row for a content is the one to keep.
            if branch is not None and (file_path, blob, symbol[0], line) in seen:
                continue
            seen.add((file_path, blob, symbol[0], line))
            place = Place(file_path, oid, blob, commit, branch=branch, branch_date=date)
            hits.append(SymbolHit(place, *symbol))

        return hits


@dataclass(frozen=True)
class PackageSymbol:
    """One definition in an installed dependency. path is relative to the package's folder."""

    package: int
    path: str
    name: str
    kind: str
    parent: str | None
    line: int
    end_line: int
    signature: str


class DependencySession(_Session):
    """One open connection to the index of installed dependencies, which every repository shares."""

    def __init__(self, db: sqlite3.Connection) -> None:
        super().__init__(db, DEPENDENCY_SCHEMA, DEPENDENCY_SCHEMA_VERSION)

    def package_row(self, ecosystem: str, name: str, version: str) -> int | None:
        """The row id of one version of a package, or None when it isn't indexed yet."""
        row = self._db.execute(
            "SELECT id FROM packages WHERE ecosystem = ? AND name = ? AND version = ?", (ecosystem, name, version)
        ).fetchone()
        return row[0] if row else None

    def insert_package(
        self,
        ecosystem: str,
        name: str,
        version: str,
        files: dict[str, list[Symbol]],
    ) -> int:
        """Store one version of a package, with each of its source files  and the symbols in it. Returns its row id."""
        insert = "INSERT INTO packages (ecosystem, name, version) VALUES (?, ?, ?) RETURNING id"
        package: int = self._db.execute(insert, (ecosystem, name, version)).fetchone()[0]

        self._db.executemany(
            "INSERT INTO package_files (package, path) VALUES (?, ?)", [(package, path) for path in files]
        )
        self._db.executemany(
            "INSERT INTO package_symbols (package, path, name, kind, parent, line, end_line, signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (package, path, s.name, s.kind, s.parent, s.line, s.end_line, s.signature)
                for path, symbols in files.items()
                for s in symbols
            ],
        )

        return package

    def package_files(self, packages: list[int]) -> list[tuple[int, str]]:
        """Every source file of these packages, as (package row, path)."""
        sql = f"SELECT package, path FROM package_files WHERE package IN ({_marks(packages)}) ORDER BY package, path"
        return self._db.execute(sql, packages).fetchall()

    def symbols_named(
        self,
        name: str,
        parent: str | None,
        kind: str | None,
        packages: list[int],
    ) -> list[PackageSymbol]:
        """Symbols with exactly this name, ignoring case."""
        return self._matching_symbols("name = ?", [name], parent, kind, packages)

    def symbols_starting_with(
        self,
        prefix: str,
        parent: str | None,
        kind: str | None,
        packages: list[int],
    ) -> list[PackageSymbol]:
        """Symbols whose name starts with this text."""
        return self._matching_symbols("name LIKE ? ESCAPE '\\'", [_like_escape(prefix) + "%"], parent, kind, packages)

    def symbols_containing_all(
        self,
        words: list[str],
        parent: str | None,
        kind: str | None,
        packages: list[int],
    ) -> list[PackageSymbol]:
        """Symbols whose name contains every word."""
        condition = " AND ".join("name LIKE ? ESCAPE '\\'" for _ in words)
        return self._matching_symbols(
            condition, ["%" + _like_escape(word) + "%" for word in words], parent, kind, packages
        )

    def _matching_symbols(
        self,
        condition: str,
        parameters: list[str],
        parent: str | None,
        kind: str | None,
        packages: list[int],
    ) -> list[PackageSymbol]:
        values: list[str | int] = [*parameters]
        if parent:
            condition += " AND parent = ?"
            values.append(parent)
        if kind:
            condition += " AND kind = ?"
            values.append(kind.lower())

        sql = f"""
            SELECT package, path, name, kind, parent, line, end_line, signature FROM package_symbols
            WHERE {condition} AND package IN ({_marks(packages)})
            LIMIT {TIER_FETCH}
        """
        return [PackageSymbol(*row) for row in self._db.execute(sql, [*values, *packages])]


@contextmanager
def connect(path: Path) -> Generator[IndexSession]:
    """Open the index. When the file is new or has an old schema version, this creates the tables first."""
    with _open(path) as db:
        yield IndexSession(db)


@contextmanager
def connect_dependencies(path: Path) -> Generator[DependencySession]:
    """Open the shared index of dependencies, creating the tables first when it is new or has an old schema version."""
    with _open(path) as db:
        yield DependencySession(db)


@contextmanager
def _open(path: Path) -> Generator[sqlite3.Connection]:
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        yield db
    finally:
        db.close()


def _newest_per_content(places: list[Place]) -> list[Place]:
    """
    The first place for each path and content, in the order given. Most branches share most of their files, so one
    content is often on many branches.
    """
    seen: set[tuple[str, int]] = set()
    kept: list[Place] = []

    for place in places:
        if (place.path, place.blob_row) not in seen:
            seen.add((place.path, place.blob_row))
            kept.append(place)

    return kept


def age(timestamp: int) -> str:
    """How long ago a time was, in its largest whole unit, such as "3 days ago" or "5 months ago"."""
    seconds = max(0, int(time.time()) - timestamp)

    for unit, length in [("year", 365 * 86400), ("month", 30 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60)]:
        if seconds >= length:
            count = seconds // length
            return f"{count} {unit} ago" if count == 1 else f"{count} {unit}s ago"

    return "just now"


def path_matches(path: str, path_filter: str | None) -> bool:
    """True when path contains the filter, or matches it as a glob when it has a wildcard."""
    if not path_filter:
        return True
    if any(wildcard in path_filter for wildcard in "*?["):
        return fnmatch.fnmatch(path, path_filter) or fnmatch.fnmatch(path, "*/" + path_filter)
    return path_filter.lower() in path.lower()


def _path_sql(path_filter: str | None) -> tuple[str, list[str]]:
    """The same test as path_matches, as a SQL condition on a column called path."""
    if not path_filter:
        return "1", []
    if any(wildcard in path_filter for wildcard in "*?["):
        return "(path GLOB ? OR path GLOB ?)", [path_filter, "*/" + path_filter]
    return "path LIKE ? ESCAPE '\\'", ["%" + _like_escape(path_filter) + "%"]


def _marks(values: list[int]) -> str:
    """One "?" for each value, for an IN list. An empty list gives NULL, which matches nothing."""
    return ", ".join("?" for _ in values) or "NULL"


def _in_order(parts: list[str]) -> str:
    return "%" + "%".join(_like_escape(part) for part in parts) + "%"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
