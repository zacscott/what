"""
The tables of the index, one class for each, and the SQL that reads and writes them. No other module holds SQL.

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


@contextmanager
def connect(path: Path) -> Generator[sqlite3.Connection]:
    """Open the index. When the file is new or has an old schema version, this creates the tables first."""
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")

        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            _create_tables(db)
        yield db
    finally:
        db.close()


@contextmanager
def write_transaction(db: sqlite3.Connection) -> Generator[None]:
    """
    Run the block in one transaction. The transaction takes the write lock at its
    start, so no other process can write between this one's reads and writes.
    """
    db.execute("BEGIN IMMEDIATE")
    yield
    db.execute("COMMIT")


def _create_tables(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        # Another process may have created the tables while this one waited for the lock.
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for name in ["blobs", "symbols", "symbol_names", "files", "deleted", "text_fts", "meta"]:
                db.execute(f"DROP TABLE IF EXISTS {name}")

            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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

    @classmethod
    def at_path(cls, db: sqlite3.Connection, path: str) -> list["Place"]:
        """The file at exactly this path, as a list of one, or empty when there is none."""
        return cls._where(db, "path = ?", [path])

    @classmethod
    def ending_with(cls, db: sqlite3.Connection, path: str) -> list["Place"]:
        """Every file whose path ends with this one after a slash, so "a.py" matches "src/a.py"."""
        return cls._where(db, "path LIKE ? ESCAPE '\\'", ["%/" + _like_escape(path)])

    @classmethod
    def path_containing_all(cls, db: sqlite3.Connection, words: list[str]) -> list["Place"]:
        """Files whose path contains every word."""
        condition = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in words)
        return cls._where(db, condition, ["%" + _like_escape(word) + "%" for word in words])

    @classmethod
    def path_containing_in_order(cls, db: sqlite3.Connection, parts: list[str]) -> list["Place"]:
        """Files whose payh contains every part, in order, with anything between."""
        return cls._where(db, "path LIKE ? ESCAPE '\\'", [_in_order(parts)])

    @classmethod
    def live_with_text(cls, db: sqlite3.Connection, literals: list[str]) -> list["Place"]:
        """
        Live files whose text contains every literal, best match first. With no
        literals, every live file is a candidate.
        """
        return cls._with_text(db, literals, "files")

    @classmethod
    def deleted_with_text(cls, db: sqlite3.Connection, literals: list[str]) -> list["Place"]:
        """
        Deleted files whose text contains every literal, best match first. With no
        literals, every deleted file is a candidate.
        """
        return cls._with_text(db, literals, "deleted")

    @classmethod
    def _where(cls, db: sqlite3.Connection, condition: str, parameters: list[str]) -> list["Place"]:
        """Files matching a condition on path. A path that exists now is never also listed as deleted."""
        sql = f"""
            SELECT f.path, b.oid, b.id, NULL FROM files f JOIN blobs b ON b.id = f.blob WHERE {condition}
            UNION ALL
            SELECT d.path, b.oid, b.id, d.commit_id FROM deleted d JOIN blobs b ON b.id = d.blob
            WHERE {condition} AND d.path NOT IN (SELECT path FROM files)
        """

        return [cls(*row) for row in db.execute(sql, parameters + parameters)]

    @classmethod
    def _with_text(cls, db: sqlite3.Connection, literals: list[str], table: str) -> list["Place"]:
        history = "t.commit_id" if table == "deleted" else "NULL"
        still_gone = "AND t.path NOT IN (SELECT path FROM files)" if table == "deleted" else ""

        if not literals:
            sql = f"SELECT t.path, b.oid, b.id, {history} FROM {table} t JOIN blobs b ON b.id = t.blob ORDER BY t.path"
            return [cls(*row) for row in db.execute(sql)]

        match = " AND ".join('"' + literal.replace('"', '""') + '"' for literal in literals)
        sql = f"""
            SELECT t.path, b.oid, b.id, {history}
            FROM text_fts JOIN {table} t ON t.blob = text_fts.rowid JOIN blobs b ON b.id = t.blob
            WHERE text_fts MATCH ? {still_gone}
            ORDER BY bm25(text_fts), t.path
        """

        return [cls(*row) for row in db.execute(sql, (match,))]


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

    @classmethod
    def named(
        cls,
        db: sqlite3.Connection,
        name: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list["SymbolHit"]:
        """Symbols with exactly this name, ignoring case."""
        return cls._matching(db, "s.name = ?", [name], parent, kind, path)

    @classmethod
    def starting_with(
        cls,
        db: sqlite3.Connection,
        prefix: str,
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list["SymbolHit"]:
        """Symbols whose name starts with this text."""
        return cls._matching(db, "s.name LIKE ? ESCAPE '\\'", [_like_escape(prefix) + "%"], parent, kind, path)

    @classmethod
    def containing_all(
        cls,
        db: sqlite3.Connection,
        words: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list["SymbolHit"]:
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

        return cls._matching(db, " AND ".join(conditions), parameters, parent, kind, path)

    @classmethod
    def containing_in_order(
        cls,
        db: sqlite3.Connection,
        parts: list[str],
        parent: str | None,
        kind: str | None,
        path: str | None,
    ) -> list["SymbolHit"]:
        """Symbols whose name contains every part, in order, with anything between."""
        return cls._matching(db, "s.name LIKE ? ESCAPE '\\'", [_in_order(parts)], parent, kind, path)

    @classmethod
    def in_blob(cls, db: sqlite3.Connection, blob: int) -> list["SymbolHit"]:
        """
        Every symbol in one file's content, in line order, outer symbols first.
        Their place holds only the blob row.
        """
        rows = db.execute(
            "SELECT name, kind, parent, line, end_line, signature FROM symbols WHERE blob = ? ORDER BY line, end_line DESC",
            (blob,),
        )

        place = Place("", "", blob)
        return [cls(place, *row) for row in rows]

    @classmethod
    def _matching(
        cls,
        db: sqlite3.Connection,
        condition: str,
        parameters: list[str],
        parent: str | None,
        kind: str | None,
        path_filter: str | None,
    ) -> list["SymbolHit"]:
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
            cls(Place(file_path, oid, blob, commit), name, kind, parent, line, end_line, signature)
            for file_path, commit, name, kind, parent, line, end_line, signature, oid, blob in db.execute(
                sql, parameters + parameters + path_parameters
            )
        ]


class Blob:
    """
    One file content, keyed on git's blob id and how it was parsed. Its text and
    symbols are stored under its row id.
    """

    @staticmethod
    def find(db: sqlite3.Connection, oid: str, parsed_as: str) -> int | None:
        """The row id of a content parsed this way, or None when the index hasn't seen it."""
        row = db.execute("SELECT id FROM blobs WHERE oid = ? AND parsed_as = ?", (oid, parsed_as)).fetchone()
        return row[0] if row else None

    @staticmethod
    def add(
        db: sqlite3.Connection,
        oid: str,
        parsed_as: str,
        text: str | None,
        symbols: list[Symbol],
    ) -> int:
        """Store a new content and return its row id. When text is None, only the row is stored."""
        insert = "INSERT INTO blobs (oid, parsed_as) VALUES (?, ?) RETURNING id"
        blob: int = db.execute(insert, (oid, parsed_as)).fetchone()[0]

        if text is not None:
            db.execute("INSERT INTO text_fts (rowid, body) VALUES (?, ?)", (blob, text))
            db.executemany(
                "INSERT INTO symbols (blob, name, kind, parent, line, end_line, signature) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(blob, s.name, s.kind, s.parent, s.line, s.end_line, s.signature) for s in symbols],
            )
            db.execute("INSERT INTO symbol_names (rowid, name) SELECT rowid, name FROM symbols WHERE blob = ?", (blob,))

        return blob

    @staticmethod
    def delete_unused(db: sqlite3.Connection) -> None:
        """Delete every content that no live or deleted file holds, wiht its text and symbols."""
        unused = (
            "SELECT id FROM blobs WHERE id NOT IN (SELECT blob FROM files) AND id NOT IN (SELECT blob FROM deleted)"
        )

        with write_transaction(db):
            # An index that reads its text from symbols must be told what each row held before the row goes.
            db.execute(
                "INSERT INTO symbol_names (symbol_names, rowid, name) "
                f"SELECT 'delete', rowid, name FROM symbols WHERE blob IN ({unused})"
            )
            db.execute(f"DELETE FROM symbols WHERE blob IN ({unused})")
            db.execute(f"DELETE FROM text_fts WHERE rowid IN ({unused})")
            db.execute(f"DELETE FROM blobs WHERE id IN ({unused})")


class File:
    """
    The content each path in the working tree holds now. The stat is set only for
    files git reports as changed or untracked.
    """

    @staticmethod
    def known(db: sqlite3.Connection) -> dict[str, tuple[str, str | None]]:
        """Every path in the table, with its blob id and stat."""
        rows = db.execute("SELECT f.path, b.oid, f.stat FROM files f JOIN blobs b ON b.id = f.blob")
        return {path: (oid, stat) for path, oid, stat in rows}

    @staticmethod
    def save(db: sqlite3.Connection, path: str, blob: int, stat: str | None) -> None:
        """Record the content a path holds, replacing what it held before."""
        db.execute("INSERT OR REPLACE INTO files (path, blob, stat) VALUES (?, ?, ?)", (path, blob, stat))

    @staticmethod
    def delete(db: sqlite3.Connection, paths: list[str]) -> None:
        """Forget these paths. Their blobs stay until Blob.delete_unused runs."""
        db.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in paths])


class DeletedFile:
    """The content each path held just before a commit deleted it."""

    @staticmethod
    def save(db: sqlite3.Connection, path: str, blob: int, commit: str, date: str) -> None:
        """Record the content a deleted path held and the commit that deleted it, replacing ayn earlier record."""
        db.execute(
            "INSERT OR REPLACE INTO deleted (path, blob, commit_id, date) VALUES (?, ?, ?, ?)",
            (path, blob, commit, date),
        )

    @staticmethod
    def delete_all(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM deleted")


class Meta:
    """Values the index keeps about itself, such as the HEAD commit its deleted files were read up to."""

    @staticmethod
    def get(db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def set(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


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
