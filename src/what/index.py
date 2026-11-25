"""
The SQLite index of one repository, and the code that keeps it up to date.
The tables, and the SQL for them, are in models.
"""

import fcntl
import fnmatch
import hashlib
import os
import stat as stat_modes
import subprocess
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal, NamedTuple

from what import directory, git, models
from what.config import settings
from what.models import BranchListing, IndexSession
from what.symbols import Symbol, extract_many

# The lonng builds that take a lock, so only one process runs each at a time.
type LockName = Literal["history", "branches"]


class StaleFile(NamedTuple):
    """
    A file in the working tree that the index must read again. oid is set for a file git tracks unchanged, and stat
    for a file whose blob id has to be worked out from its content.
    """

    path: str
    oid: str | None
    stat: str | None


class BranchState(NamedTuple):
    """The other branches, and how many of them are still to be read."""

    branches: list[git.Branch]
    pending: int


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
        # The folder that holds this index, and the index of dependencies that every repository shares.
        self.cache_dir = cache_dir

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
            stale: list[StaleFile] = []
            present: set[str] = set()

            for path in unhashed:
                stat = self._stat(path)
                if stat is None:
                    continue
                present.add(path)
                if path not in known or known[path].stat != stat:
                    stale.append(StaleFile(path, None, stat))

            for path, oid in tracked.items():
                if path in unhashed:
                    continue
                present.add(path)
                if path not in known or known[path].oid != oid:
                    stale.append(StaleFile(path, oid, None))

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

        with self.connect() as session, self._lock("history") as locked:
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

    def refresh_branches(self, most: int | None = None) -> BranchState:
        """
        Index the files that each other branch changes. Returns the branches, and how many of them are still to be
        read. When more than most branches have moved, none are read here, so a search doesn't wait minutes on a
        repository with a thousand branches.

        The default branch is compared with HEAD, so it shows what HEAD lacks, and is read again whenever HEAD moves.
        Every other branch is compared with where it split from the default branch, so it shows only its own work,
        and is read again only when it moves.
        """
        current = git.head(self.root) if self.in_git else None
        if current is None:
            return BranchState([], 0)

        oldest = time.time() - settings.BRANCH_MAX_AGE_DAYS * 86400
        branches = [branch for branch in git.other_branches(self.root) if branch.is_default or branch.date >= oldest]
        default = next((branch.tip for branch in branches if branch.is_default), None)

        with self.connect() as session, self._lock("branches") as locked:
            known = session.known_branches()
            stale = [
                branch
                for branch in branches
                if known.get(branch.name) != BranchListing(branch.tip, _base_key(branch, current))
            ]
            if not locked or (most is not None and len(stale) > most):
                return BranchState(branches, len(stale))

            def changed_files(branch: git.Branch) -> dict[str, str]:
                base = current if branch.is_default else git.merge_base(self.root, default or current, branch.tip)
                return git.changed_files(self.root, base, branch.tip) if base else {}

            # Each branch needs two git commands, and they spend most of their time waiting on the disk.
            with ThreadPoolExecutor(settings.GIT_THREADS) as threads:
                for branch, changed in zip(stale, threads.map(changed_files, stale)):
                    self._index_branch(session, branch, _base_key(branch, current), sorted(changed.items()))

            gone = [name for name in known if name not in {branch.name for branch in branches}]
            if gone:
                with session.write_transaction():
                    session.delete_branches(gone)
            if stale or gone:
                session.delete_unused_blobs()

        return BranchState(branches, 0)

    def build_in_background(self) -> None:
        """
        Start building the whole index in a process that outlives this one, unless a build is running already.
        Reading every deleted file, or every branch of a large repository, can take minutes, so a search never
        waits for it.
        """
        if self.history_is_building() or self._is_locked("branches"):
            return

        subprocess.Popen(
            [sys.executable, "-m", "what", "--index"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # A new session keeps the build alive when the harness kills the shell that ran the search.
            start_new_session=True,
        )

    def history_is_current(self) -> bool:
        if not self.in_git:
            return True

        with self.connect() as session:
            return session.meta("history_head") == git.head(self.root)

    def history_is_building(self) -> bool:
        """True when another process holds the lock that refresh_history takes."""
        return self._is_locked("history")

    def read_live(self, path: str) -> str | None:
        """The text of a file in the working tree, or None when it is binary, too big or gone."""
        return self.read_text(self.root / path)

    def read_text(self, path: Path) -> str | None:
        """The text of a file on disk, or None when it is binary, too big or gone."""
        try:
            content = path.read_bytes()
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
        batch: list[StaleFile],
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
            parsed = _parse_new(session, [(path, oid, text) for path, oid, _, text in read])
            for path, oid, stat, text in read:
                session.save_file(path, _add_blob(session, oid, path, text, parsed), stat)

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
                # A branch's text isn't indexed. It would make the index several times bigger, and a text search on
                # branches reads the few files it needs from git.
                blobs = self._add_git_blobs(session, batch, index_text=False)
                session.save_branch_files(branch.name, [(path, blob) for (path, _), blob in zip(batch, blobs)])

        with session.write_transaction():
            session.save_branch(branch.name, branch.tip, head, branch.date)

    def _add_git_blobs(
        self,
        session: IndexSession,
        files: list[tuple[str, str]],
        index_text: bool = True,
    ) -> list[int]:
        """
        Index file contents that git holds, given as (path, blob id), and return their blob rows in the same order.
        Only the contents the index hasn't seen, or holds without the text it now needs, are read. When index_text
        is False, the symbols are indexed and the text is left out. Call this inside a write transaction.
        """
        unread: list[str] = []
        for path, oid in files:
            if not _has_indexed_text(path):
                continue
            known = session.blob_row(oid, _parsed_as(path))
            if known is None or (index_text and session.lacks_text(known)):
                unread.append(oid)

        sizes = git.blob_sizes(self.root, unread)
        small = [oid for oid in dict.fromkeys(unread) if sizes.get(oid, settings.MAX_BYTES + 1) <= settings.MAX_BYTES]

        texts = {oid: _as_text(content) for oid, content in git.read_blobs(self.root, small) if content is not None}
        contents = [(path, oid, texts.get(oid) if _has_indexed_text(path) else None) for path, oid in files]
        parsed = _parse_new(session, contents)

        return [_add_blob(session, oid, path, text, parsed, index_text) for path, oid, text in contents]

    def _is_locked(self, name: LockName) -> bool:
        """True when another process holds one of the locks that _lock takes."""
        with self._lock(name) as locked:
            return not locked

    @contextmanager
    def _lock(self, name: LockName) -> Generator[bool]:
        """
        Yield True when this process may run a long build, "history" or "branches". Several searches can
        run at once in one repository, and only one build should read every deleted file or every branch.
        """
        suffix = ".lock" if name == "history" else f".{name}.lock"
        with open(self.path.with_suffix(suffix), "w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True


def _base_key(branch: git.Branch, head: str) -> str:
    """
    What a branch's listing of files depends on besides its tip: HEAD for the default branch, and nothing for the
    others, which are compared with the default branch.
    """
    return head if branch.is_default else ""


def _parse_new(
    session: IndexSession,
    contents: list[tuple[str, str, str | None]],
) -> dict[tuple[str, str], list[Symbol]]:
    """
    The symbols of each content, given as (path, blob id, text), that the index hasn't seen, by (blob id, parsed_as).
    They are parsed together, so a big batch uses every core.
    """
    new: dict[tuple[str, str], tuple[str, str]] = {}
    for path, oid, text in contents:
        key = (oid, _parsed_as(path))
        if text is not None and key not in new and session.blob_row(*key) is None:
            new[key] = (path, text)

    return dict(zip(new, extract_many(list(new.values()))))


def _add_blob(
    session: IndexSession,
    oid: str,
    path: str,
    text: str | None,
    parsed: dict[tuple[str, str], list[Symbol]],
    index_text: bool = True,
) -> int:
    """
    Return the row id of a file content, indexing it when the index hasn't  seen it. parsed holds the symbols
    _parse_new found. A content stored without its text gets the text when index_text asks for it.
    """
    parsed_as = _parsed_as(path)
    known = session.blob_row(oid, parsed_as)
    if known is not None:
        if index_text and text is not None and session.lacks_text(known):
            session.add_text(known, text)
        return known

    symbols = parsed.get((oid, parsed_as), []) if text is not None else []
    return session.insert_blob(oid, parsed_as, text, symbols, index_text)


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
