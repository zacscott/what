"""
The tables of the index, one open connection to them, and the SQL that reads and writes them. No other
module holds SQL.

A row in blobs is one file content, keyed on git's blob id. The files table
says which content each path holds now. The deleted table says which content
a deleted path held last. The symbols and text_fts tables hold what each blob's text was indexed as.
"""

import fnmatch
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from what.symbols import Symbol

# Changing the tables means raising this. An index with another version is rebuilt from nothing.
SCHEMA_VERSION = 3

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


@dataclass(frozen=True)
class Place:
    """Where a file's content comes from. commit is set only for a deleted file."""

    path: str
    oid: str
    blob_row: int
    commit: str | None = None

    @property
    def ref(self) -> str:
        """
        How output names the file. A deleted file is named the way git names a file at a commit, in the commit
        before the one that deleted it, such as "a1b2c3d4^:legacy/split.py".
        """
        if self.commit is None:
            return self.path

        return f"{self.commit[:8]}^:{self.path}"


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


class IndexSession:
    """One open connection to the index, with the queries that read and write it."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._ensure_schema()

    @contextmanager
    def write_transaction(self) -> Generator[None]:
        """
        Run the block in one transaction. The transaction takes the write lock at its
        start, so no other process can write between this one's reads and writes.
        """
        self._db.execute("BEGIN IMMEDIATE")
        yield
        self._db.execute("COMMIT")

    def places_at_path(self, path: str) -> list[Place]:
        """The file at exactly this path, as a list of one, or empty when there is none."""
        return self._places_where("path = ?", [path])

    def places_ending_with(self, path: str) -> list[Place]:
        """Every file whose path ends with this one after a slash, so "a.py" matches "src/a.py"."""
        return self._places_where("path LIKE ? ESCAPE '\\'", ["%/" + _like_escape(path)])

    def places_containing_all(self, words: list[str]) -> list[Place]:
        """Files whose path contains every word."""
        condition = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in words)
        return self._places_where(condition, ["%" + _like_escape(word) + "%" for word in words])

    def places_containing_in_order(self, parts: list[str]) -> list[Place]:
        """Files whose payh contains every part, in order, with anything between."""
        return self._places_where("path LIKE ? ESCAPE '\\'", [_in_order(parts)])

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

    def symbols_named(
        self,
        name: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list[SymbolHit]:
        """Symbols with exactly this name, ignoring case."""
        return self._matching_symbols("s.name = ?", [name], parent, kind, path)

    def symbols_starting_with(
        self,
        prefix: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list[SymbolHit]:
        """Symbols whose name starts with this text."""
        return self._matching_symbols("s.name LIKE ? ESCAPE '\\'", [_like_escape(prefix) + "%"], parent, kind, path)

    def symbols_containing_all(
        self,
        words: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
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

        return self._matching_symbols(" AND ".join(conditions), parameters, parent, kind, path)

    def symbols_containing_in_order(
        self,
        parts: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list[SymbolHit]:
        """Symbols whose name contains every part, in order, with anything between."""
        return self._matching_symbols("s.name LIKE ? ESCAPE '\\'", [_in_order(parts)], parent, kind, path)

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
            "SELECT id FROM blobs WHERE id NOT IN (SELECT blob FROM files) AND id NOT IN (SELECT blob FROM deleted)"
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

    def meta(self, key: str) -> str | None:
        """A value the index keeps about itself, or None when it has never been set."""
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Record a value the index keeps about itself."""
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _places_where(self, condition: str, parameters: list[str]) -> list[Place]:
        """Files matching a condition on path. A path that exists now is never also listed as deleted."""
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
    ) -> list[SymbolHit]:
        """Live then deleted symbols matching a condition on the symbols table, in files the path filter allows."""
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

        sql = f"""
            SELECT * FROM (
                SELECT f.path AS path, NULL, {columns}
                FROM symbols s JOIN blobs b ON b.id = s.blob JOIN files f ON f.blob = s.blob
                WHERE {condition}
                UNION ALL
                SELECT d.path AS path, d.commit_id, {columns}
                FROM symbols s JOIN blobs b ON b.id = s.blob JOIN deleted d ON d.blob = s.blob
                WHERE {condition} AND d.path NOT IN (SELECT path FROM files)
            )
            WHERE {path_condition}
            LIMIT {TIER_FETCH}
        """

        return [
            SymbolHit(Place(file_path, oid, blob, commit), name, kind, parent, line, end_line, signature)
            for file_path, commit, name, kind, parent, line, end_line, signature, oid, blob in self._db.execute(
                sql, parameters + parameters + path_parameters
            )
        ]

    def _ensure_schema(self) -> None:
        """Create the tables when the file is new or has an old schema version."""
        if self._schema_version() == SCHEMA_VERSION:
            return

        with self.write_transaction():
            # Another process may have created the tables while this one waited for the lock.
            if self._schema_version() == SCHEMA_VERSION:
                return

            for name in ["blobs", "symbols", "symbol_names", "files", "deleted", "text_fts", "meta"]:
                self._db.execute(f"DROP TABLE IF EXISTS {name}")

            for statement in SCHEMA.split(";"):
                if statement.strip():
                    self._db.execute(statement)

            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def connect(path: Path) -> Generator[IndexSession]:
    """Open the index. When the file is new or has an old schema version, this creates the tables first."""
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        yield IndexSession(db)
    finally:
        db.close()


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


def _in_order(parts: list[str]) -> str:
    return "%" + "%".join(_like_escape(part) for part in parts) + "%"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
