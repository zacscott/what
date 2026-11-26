"""
The SQLite index of one repository, and the code that keeps it up to date.
The tables, and the SQL for them, are in models.
"""

import fcntl
import fnmatch
import hashlib
import logging
import os
import stat as stat_modes
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from enum import Enum
from pathlib import Path
from typing import Literal, NamedTuple, Protocol

from what import database, directory, git, models
from what.config import settings
from what.database import IndexSession
from what.models import BranchListing
from what.symbols import Symbol, extract_many

logger = logging.getLogger(__name__)

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


# What the index reads a lot of at once.
type Reading = Literal["files", "deleted files", "branches"]


class Report(Protocol):
    """Called while the index reads a lot at once, with how far it is. The command line shows it as progress."""

    def __call__(self, reading: Reading, *, done: int, total: int) -> None: ...


def _report_nothing(reading: Reading, *, done: int, total: int) -> None:
    """The report of an index that nobody watches."""


class History(Enum):
    """Whether the index holds the files that HEAD's history deleted."""

    CURRENT = "up to date"
    BUILDING = "being read now"
    STALE = "not up to date"
    NO_GIT = "not a git repository"


class Status(NamedTuple):
    """
    What the index holds and how far its builds are. size is the bytes the index takes on disk. building is True
    while a process reads deleted files or branches. build_wait is how many seconds a search waits before it starts
    the background build again, or 0 when it would start one at once.
    """

    counts: models.Counts
    size: int
    history: History
    building: bool
    build_wait: float


class Index:
    """The index of one repository. Each call opens its own connection, so any thread can use it."""

    def __init__(
        self,
        root: Path,
        cache_dir: Path | None = None,
        *,
        report: Report = _report_nothing,
    ) -> None:
        # Whether the index was built from git. In directory mode there is no history, so there are no deleted files
        # or commits.
        self.in_git = True
        # The file git keeps its list of staged files in, which changes whenever that list does.
        self._git_index_file: Path | None = None
        try:
            self.root, self._git_index_file = git.repository(root)
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
        self._report = report

    def connect(self) -> AbstractContextManager[IndexSession]:
        """Open the index. When the file is new or has an old schema version, this creates the tables first."""
        return database.connect(self.path)

    def refresh(self) -> None:
        """
        Make the index match the working tree, uncommitted edits included. Every
        search calls this first, so it only reads the files that changed.
        """
        staged = self._git_index_stat()
        # A file git reports as changed is hashed here, because its blob id in git's index is out of date.
        unhashed = git.unstaged_files(self.root) if self.in_git else set(directory.files(self.root))

        with self.connect() as session:
            # When git's list of staged files is the one the last full refresh read, the tracked files that haven't
            # changed are already indexed. Reading that list takes most of the time of a search that changes nothing.
            if (
                staged is not None
                and staged == session.meta("git_index")
                and staged == self._git_index_stat()
                and self._refresh_unstaged(session, unhashed)
            ):
                return

            tracked = git.tracked_files(self.root) if self.in_git else {}
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
                # A file that was read from disk and is staged now keeps its content but must lose its stat. With
                # the stat, every later search would take it for an unstaged file that went missing.
                if path not in known or known[path].oid != oid or known[path].stat is not None:
                    stale.append(StaleFile(path, oid, None))

            self._index_stale(session, stale)

            removed = [path for path in known if path not in present]
            if removed:
                session.delete_files(removed)

            if stale or removed:
                session.delete_unused_blobs()
                logger.info("%s: read %d files again and forgot %d", self.root, len(stale), len(removed))
            if staged is not None:
                session.set_meta("git_index", staged)

    def _refresh_unstaged(self, session: IndexSession, unhashed: set[str]) -> bool:
        """
        Index again the unstaged files that changed, without reading git's list of tracked files. Returns False,
        and changes nothing, when only that list can say what a file holds now.
        """
        known = session.files_read_from_disk()
        stats = {path: self._stat(path) for path in unhashed}

        # A file the index read from disk that git no longer lists as unstaged was deleted, staged or changed back.
        if any(stats.get(path) is None for path in known):
            return False

        # An unstaged file that is gone from disk must leave the index, when the index still holds it.
        if any(stat is None and session.has_file(path) for path, stat in stats.items()):
            return False

        stale = [
            StaleFile(path, None, stat) for path, stat in stats.items() if stat is not None and known.get(path) != stat
        ]
        self._index_stale(session, stale)
        if stale:
            session.delete_unused_blobs()
            logger.info("%s: read %d unstaged files again", self.root, len(stale))

        return True

    def _index_stale(self, session: IndexSession, stale: list[StaleFile]) -> None:
        """Read the stale files again, one batch to a transaction, so a search in another process waits for one batch."""
        for start in range(0, len(stale), settings.BATCH_SIZE):
            self._report("files", done=start, total=len(stale))
            self._index_files(session, stale[start : start + settings.BATCH_SIZE])

    def _git_index_stat(self) -> str | None:
        """
        A value that changes whenever git writes its list of staged files. None outside git, and when the file is
        missing, as in a repository with nothing staged yet.
        """
        if self._git_index_file is None:
            return None

        try:
            stat = os.stat(self._git_index_file)
        except OSError:
            return None

        return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"

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

            since = done if done and git.is_ancestor(self.root, older=done, newer=current) else None

            latest: dict[str, git.Deletion] = {}
            for deletion in git.deletions(self.root, since):
                latest.setdefault(deletion.path, deletion)

            pending = list(latest.values())

            for start in range(0, len(pending), settings.BATCH_SIZE):
                self._report("deleted files", done=start, total=len(pending))
                self._index_deletions(session, pending[start : start + settings.BATCH_SIZE])

            if since is None:
                # HEAD moved to another line of history, so the paths its history never deleted are forgotten here,
                # after the new ones are saved. Forgetting every path first leaves their blobs unused, so a search in
                # between deletes them and the build reads them all from git again.
                with session.write_transaction():
                    session.delete_deleted_files(session.deleted_paths() - latest.keys())

            session.delete_unused_blobs()
            session.set_meta("history_head", current)
            logger.info("%s: read %d deleted files up to commit %s", self.root, len(pending), current[:8])

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
                base = (
                    current
                    if branch.is_default
                    else git.merge_base(self.root, first=default or current, second=branch.tip)
                )
                return git.changed_files(self.root, base=base, tip=branch.tip) if base else {}

            # Only a branch that moved needs threads, so they are imported here, and a search doesn't pay for the
            # import. Each branch needs two git commands, and they spend most of their time waiting on the disk.
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(settings.GIT_THREADS) as threads:
                for done, (branch, changed) in enumerate(zip(stale, threads.map(changed_files, stale))):
                    self._report("branches", done=done, total=len(stale))
                    self._index_branch(session, branch, head=_base_key(branch, current), files=sorted(changed.items()))

            gone = [name for name in known if name not in {branch.name for branch in branches}]
            if gone:
                with session.write_transaction():
                    session.delete_branches(gone)
            if stale or gone:
                session.delete_unused_blobs()
                logger.info("%s: read %d branches again and forgot %d", self.root, len(stale), len(gone))

        return BranchState(branches, 0)

    def build_in_background(self) -> None:
        """
        Start building the whole index in a process that outlives this one, unless a build is running already.
        Reading every deleted file, or every branch of a large repository, can take minutes, so a search never
        waits for it.
        """
        if self.history_is_building() or self._is_locked("branches"):
            return

        with self.connect() as session:
            # A build sets this time when it starts and again when it fails, and removes it when it finishes. So
            # a time still to come means a build has just started, or one failed or was killed not long ago.
            if time.time() < float(session.meta("build_wait_until") or 0):
                logger.info("%s: a background build started or failed not long ago, so none is started", self.root)
                return
            session.set_meta("build_wait_until", str(time.time() + settings.BUILD_RETRY_SECONDS))

        logger.info("%s: starting the background build", self.root)
        subprocess.Popen(
            # The folder is named, because a WHAT_ROOT the build inherits could name another one.
            [sys.executable, "-m", "what", "-C", str(self.root), "index"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # A new session keeps the build alive when the harness kills the shell that ran the search.
            start_new_session=True,
        )

    def build_finished(self) -> None:
        """Record that the whole index was built, so the next search may start another build at once."""
        with self.connect() as session:
            session.delete_meta("build_wait_until")

        logger.info("%s: the whole index is built", self.root)

    def build_failed(self) -> None:
        """Record that a build of the whole index failed, so no search starts another one for a while."""
        with self.connect() as session:
            session.set_meta("build_wait_until", str(time.time() + settings.BUILD_RETRY_SECONDS))

    def history_is_current(self) -> bool:
        if not self.in_git:
            return True

        with self.connect() as session:
            return session.meta("history_head") == git.head(self.root)

    def history_is_building(self) -> bool:
        """True when another process holds the lock that refresh_history takes."""
        return self._is_locked("history")

    def status(self) -> Status:
        """What the index holds and how far its builds are. Nothing is read again to answer this."""
        with self.connect() as session:
            counts = session.counts()
            build_wait = max(0.0, float(session.meta("build_wait_until") or 0) - time.time())

        if not self.in_git:
            history = History.NO_GIT
        elif self.history_is_building():
            history = History.BUILDING
        else:
            history = History.CURRENT if self.history_is_current() else History.STALE

        # SQLite keeps the newest writes in a second file next to the index until it copies them in.
        files = [self.path, self.path.with_name(self.path.name + "-wal")]
        size = sum(file.stat().st_size for file in files if file.exists())

        building = history == History.BUILDING or self._is_locked("branches")
        return Status(counts=counts, size=size, history=history, building=building, build_wait=build_wait)

    def read_live(self, path: str) -> str | None:
        """The text of a file in the working tree, or None when it is binary, too big or gone."""
        return self.read_text(self.root / path)

    def read_text(self, path: Path) -> str | None:
        """The text of a file on disk, or None when it is binary, too big or gone."""
        try:
            content = path.read_bytes()
        except OSError:
            return None

        return as_text(content)

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
            text = as_text(content) if _has_indexed_text(path) else None
            read.append((path, oid or git.blob_id(content), stat, text))

        with session.write_transaction():
            parsed = _parse_new(session, [(path, oid, text) for path, oid, _, text in read])
            for path, oid, stat, text in read:
                session.save_file(path, blob=_add_blob(session, oid, path=path, text=text, parsed=parsed), stat=stat)

    def _index_deletions(self, session: IndexSession, batch: list[git.Deletion]) -> None:
        # The transaction starts before the check for known blobs, so another process can't delete one in between.
        with session.write_transaction():
            blobs = self._add_git_blobs(session, [(deletion.path, deletion.oid) for deletion in batch])
            for deletion, blob in zip(batch, blobs):
                session.save_deleted_file(deletion.path, blob=blob, commit=deletion.commit, date=deletion.date)

    def _index_branch(
        self,
        session: IndexSession,
        branch: git.Branch,
        *,
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
            session.save_branch(branch.name, tip=branch.tip, head=head, date=branch.date)

    def _add_git_blobs(self, session: IndexSession, files: list[tuple[str, str]]) -> list[int]:
        """
        Index file contents that git holds, given as (path, blob id), and return their blob rows in the same order.
        Only the contents the index hasn't seen are read. Call this inside a write transaction.
        """
        unread = [
            oid for path, oid in files if _has_indexed_text(path) and session.blob_row(oid, parsed_as(path)) is None
        ]

        sizes = git.blob_sizes(self.root, unread)
        small = [oid for oid in dict.fromkeys(unread) if sizes.get(oid, settings.MAX_BYTES + 1) <= settings.MAX_BYTES]

        texts = {oid: as_text(content) for oid, content in git.read_blobs(self.root, small) if content is not None}
        contents = [(path, oid, texts.get(oid) if _has_indexed_text(path) else None) for path, oid in files]
        parsed = _parse_new(session, contents)

        return [_add_blob(session, oid, path=path, text=text, parsed=parsed) for path, oid, text in contents]

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
        key = (oid, parsed_as(path))
        if text is not None and key not in new and session.blob_row(*key) is None:
            new[key] = (path, text)

    return dict(zip(new, extract_many(list(new.values()))))


def _add_blob(
    session: IndexSession,
    oid: str,
    *,
    path: str,
    text: str | None,
    parsed: dict[tuple[str, str], list[Symbol]],
) -> int:
    """
    Return the row id of a file content, indexing it when the index hasn't  seen it. parsed holds the symbols
    _parse_new found.
    """
    key = (oid, parsed_as(path))
    known = session.blob_row(*key)
    if known is not None:
        return known

    symbols = parsed.get(key, []) if text is not None else []
    return session.insert_blob(*key, text=text, symbols=symbols)


def parsed_as(path: str) -> str:
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


def as_text(content: bytes) -> str | None:
    """The content decoded, or None for a file that is too big or binary (it has a NUL byte)."""
    if len(content) > settings.MAX_BYTES or b"\0" in content:
        return None

    return content.decode("utf-8", "replace")
