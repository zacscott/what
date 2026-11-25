"""
The SQLite index of one repository, and the code that keeps it up to date.
The tables, and the SQL for them, are in models.
"""

import fcntl
import fnmatch
import hashlib
import os
import stat as stat_modes
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from what import directory, git, models
from what.config import settings
from what.models import IndexSession
from what.symbols import extract


class Index:
    """The index of one repository. Each call opens its own connection, so any thread can use it."""

    def __init__(self, root: Path, cache_dir: Path | None = None) -> None:
        # Whether the index was built from git. In directory mode there is no history, so there are no deleted files
        # or commits.
        self.in_git = True
        try:
            self.root = git.repo_root(root)
        except git.NotARepository:
            self.in_git = False
            self.root = root.resolve()

        cache_dir = cache_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "what"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Two checkouts can share a folder name, so the file name also carries a hash of the full path.
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:10]
        self.path = cache_dir / f"{self.root.name}-{digest}.db"

    def connect(self) -> AbstractContextManager[IndexSession]:
        """Open the index. When the file is new or has an old schema version, this creates the tables first."""
        return models.connect(self.path)

    def refresh(self) -> None:
        """
        Make the index match the working tree, uncommitted edits included. Every
        search calls this first, so it only reads the files that changed.
        """
        if self.in_git:
            tracked, changed, untracked = git.working_tree(self.root)
            # A file git reports as changed is hashed here, because its blob id in git's index is out of date.
            unhashed = changed | set(untracked)
        else:
            tracked = {}
            unhashed = set(directory.files(self.root))

        with self.connect() as session:
            known = session.known_files()
            # Each entry is (path, blob id, stat). The blob id is None until the file is read.
            stale: list[tuple[str, str | None, str | None]] = []
            present: set[str] = set()

            for path in unhashed:
                stat = self._stat(path)
                if stat is None:
                    continue
                present.add(path)
                if path not in known or known[path][1] != stat:
                    stale.append((path, None, stat))

            for path, oid in tracked.items():
                if path in unhashed:
                    continue
                present.add(path)
                if path not in known or known[path][0] != oid:
                    stale.append((path, oid, None))

            for start in range(0, len(stale), settings.BATCH_SIZE):
                self._index_files(session, stale[start : start + settings.BATCH_SIZE])

            removed = [path for path in known if path not in present]
            if removed:
                session.delete_files(removed)

            if stale or removed:
                session.delete_unused_blobs()

    def refresh_history(self) -> None:
        """
        Index every file that HEAD's history deleted, as it was just before the
        deletion. When HEAD has only moved forward, this reads only the new commits.
        """
        if not self.in_git:
            return

        current = git.head(self.root)

        with self.connect() as session, self._history_lock() as locked:
            done = session.meta("history_head")
            if not locked or current is None or current == done:
                return

            since = done if done and git.is_ancestor(self.root, done, current) else None
            if done and since is None:
                # HEAD moved to another line of history, so the old list of deletions no longer applies.
                session.delete_all_deleted_files()

            latest: dict[str, git.Deletion] = {}
            for deletion in git.deletions(self.root, since):
                latest.setdefault(deletion.path, deletion)

            pending = list(latest.values())

            for start in range(0, len(pending), settings.BATCH_SIZE):
                self._index_deletions(session, pending[start : start + settings.BATCH_SIZE])

            session.delete_unused_blobs()
            session.set_meta("history_head", current)

    def refresh_branches(self) -> list[git.Branch]:
        """
        Index the files that each other branch changes, and return the branches. A branch is read again only when
        it or HEAD has moved since it was last read.

        The default branch is compared with HEAD, so it shows what HEAD lacks. Every other branch is compared with
        where it split from HEAD, so it shows only that branch's own work.
        """
        current = git.head(self.root) if self.in_git else None
        if current is None:
            return []

        branches = git.other_branches(self.root)

        with self.connect() as session:
            known = session.known_branches()
            for branch in branches:
                if known.get(branch.name) == (branch.tip, current):
                    continue

                base = current if branch.is_default else git.merge_base(self.root, current, branch.tip)
                changed = git.changed_files(self.root, base, branch.tip) if base else {}
                self._index_branch(session, branch, current, sorted(changed.items()))

            gone = [name for name in known if name not in {branch.name for branch in branches}]
            if gone:
                with session.write_transaction():
                    session.delete_branches(gone)
            session.delete_unused_blobs()

        return branches

    def history_is_current(self) -> bool:
        if not self.in_git:
            return True

        with self.connect() as session:
            return session.meta("history_head") == git.head(self.root)

    def history_is_building(self) -> bool:
        """True when another process holds the lock that refresh_history takes."""
        with self._history_lock() as locked:
            return not locked

    def read_live(self, path: str) -> str | None:
        """The text of a file in the working tree, or None when it is binary, too big or gone."""
        try:
            content = (self.root / path).read_bytes()
        except OSError:
            return None

        return _as_text(content)

    def _stat(self, path: str) -> str | None:
        """
        A value that changes whenever the file is written. None when the path is
        gone or is not a regular file. A symlink could point outside the repository.
        """
        try:
            stat = os.lstat(os.path.join(self.root, path))
        except OSError:
            return None

        if not stat_modes.S_ISREG(stat.st_mode):
            return None

        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def _index_files(
        self,
        session: IndexSession,
        batch: list[tuple[str, str | None, str | None]],
    ) -> None:
        # Reading and hashing happens before the transaction, so the write lock is held only for the inserts.
        read: list[tuple[str, str, str | None, str | None]] = []
        for path, oid, stat in batch:
            try:
                content = (self.root / path).read_bytes()
            except OSError:
                continue
            text = _as_text(content) if _has_indexed_text(path) else None
            read.append((path, oid or git.blob_id(content), stat, text))

        with session.write_transaction():
            for path, oid, stat, text in read:
                session.save_file(path, _add_blob(session, oid, path, text), stat)

    def _index_deletions(self, session: IndexSession, batch: list[git.Deletion]) -> None:
        # The transaction starts before the check for known blobs, so another process can't delete one in between.
        with session.write_transaction():
            blobs = self._add_git_blobs(session, [(deletion.path, deletion.oid) for deletion in batch])
            for deletion, blob in zip(batch, blobs):
                session.save_deleted_file(deletion.path, blob, deletion.commit, deletion.date)

    def _index_branch(
        self,
        session: IndexSession,
        branch: git.Branch,
        head: str,
        files: list[tuple[str, str]],
    ) -> None:
        """
        Index the files a branch changed, as (path, blob id), and record them under the branch. Each batch's files are
        saved in the same transaction as their blobs, so another process can't delete the blobs as unused in between.
        """
        with session.write_transaction():
            session.delete_branches([branch.name])

        for start in range(0, len(files), settings.BATCH_SIZE):
            batch = files[start : start + settings.BATCH_SIZE]
            with session.write_transaction():
                blobs = self._add_git_blobs(session, batch)
                session.save_branch_files(branch.name, [(path, blob) for (path, _), blob in zip(batch, blobs)])

        with session.write_transaction():
            session.save_branch(branch.name, branch.tip, head, branch.date)

    def _add_git_blobs(self, session: IndexSession, files: list[tuple[str, str]]) -> list[int]:
        """
        Index file contents that git holds, given as (path, blob id), and return their blob rows in the same order.
        Only the contents the index hasn't seen are read. Call this inside a write transaction.
        """
        unread = [
            oid for path, oid in files if _has_indexed_text(path) and session.blob_row(oid, _parsed_as(path)) is None
        ]

        sizes = git.blob_sizes(self.root, unread)
        small = [oid for oid in dict.fromkeys(unread) if sizes.get(oid, settings.MAX_BYTES + 1) <= settings.MAX_BYTES]

        texts = {oid: _as_text(content) for oid, content in git.read_blobs(self.root, small) if content is not None}

        return [
            _add_blob(session, oid, path, texts.get(oid) if _has_indexed_text(path) else None) for path, oid in files
        ]

    @contextmanager
    def _history_lock(self) -> Generator[bool]:
        """
        Yield True when this process may build the history. Several searches can
        run at once in one repository, and only one build should read every deleted file.
        """
        with open(self.path.with_suffix(".lock"), "w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True


def _add_blob(session: IndexSession, oid: str, path: str, text: str | None) -> int:
    """Return the row id of a file content, indexing its text and symbols when the index hasn't seen it."""
    parsed_as = _parsed_as(path)
    known = session.blob_row(oid, parsed_as)
    if known is not None:
        return known

    return session.insert_blob(oid, parsed_as, text, extract(path, text) if text is not None else [])


def _parsed_as(path: str) -> str:
    """
    What decides how a file's content is indexed: its extension, or "" when its
    text is skipped. Two paths share a blobs row only when this is the same for both.
    """
    if not _has_indexed_text(path):
        return ""

    return path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()


def _has_indexed_text(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return not any(fnmatch.fnmatch(name, pattern) for pattern in settings.SKIPPED_NAMES)


def _as_text(content: bytes) -> str | None:
    """The content decoded, or None for a file that is too big or binary (it has a NUL byte)."""
    if len(content) > settings.MAX_BYTES or b"\0" in content:
        return None

    return content.decode("utf-8", "replace")
