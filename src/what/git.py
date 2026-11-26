"""The git commands the index is built from, each as one small function."""

import hashlib
import os
import subprocess
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from what.config import settings


class NotARepository(Exception):
    """The folder the command ran in is not inside a git repository."""


class UnknownRef(Exception):
    """A ref the user named is not a branch, tag or commit in the repository."""


@dataclass(frozen=True, slots=True)
class Deletion:
    """A file that a commit deleted, and the blob that held its last content."""

    path: str
    oid: str
    commit: str
    date: str


@dataclass(frozen=True, slots=True)
class Branch:
    """
    A branch other than the one checked out. date is the time of its last commit, in seconds since 1970. remote is
    True for a remote-tracking branch such as "origin/dev".
    """

    name: str
    tip: str
    date: int
    remote: bool
    is_default: bool


def run(root: Path, *args: str, stdin: bytes | None = None, timeout: float | None = None) -> bytes:
    """Run a git command in the repository and return what it printed. A failed command raises."""
    return subprocess.run(_command(root, *args), input=stdin, capture_output=True, check=True, timeout=timeout).stdout


class Repository(NamedTuple):
    """Where a repository's working tree is, and the file git keeps its list of staged files in."""

    root: Path
    index_file: Path


def repository(start: Path) -> Repository:
    """The repository a folder is inside. Raises NotARepository when there is none."""
    try:
        output = run(start, "rev-parse", "--show-toplevel", "--git-path", "index").decode()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise NotARepository(f"{start} is not inside a git repository") from error

    # git prints the index file's path relative to the folder the command ran in.
    root, index_file = output.splitlines()
    return Repository(Path(root), start / index_file)


def head(root: Path) -> str | None:
    """The commit HEAD points at, or None in a repository with no commits."""
    try:
        return run(root, "rev-parse", "--quiet", "--verify", "HEAD").decode().strip()
    except subprocess.CalledProcessError:
        return None


def unstaged_files(root: Path) -> set[str]:
    """
    The files whose content git's index holds no blob id for: the tracked files that changed since they were staged,
    and the untracked files. The two git commands run at the same time, because each one takes 10 to 40ms and
    neither depends on the other.
    """
    commands = [
        ["diff-files", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ]
    running = [
        subprocess.Popen(_command(root, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for args in commands
    ]

    found: set[str] = set()
    for process in running:
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
        found.update(_split(stdout))

    return found


def tracked_files(root: Path) -> dict[str, str]:
    """Each tracked file's path and the blob id git's index holds for it, from `git ls-files --stage -z`."""
    files: dict[str, str] = {}

    for entry in _split(run(root, "ls-files", "--stage", "-z")):
        details, path = entry.split("\t", 1)
        mode, oid, _stage = details.split(" ")
        # Submodules and symlinks have no file content of their own to index.
        if mode.startswith("100"):
            files[path] = oid

    return files


def commit_of(root: Path, ref: str) -> str:
    """The commit a ref namse, such as a branch, tag, "origin/dev" or "HEAD~3". Raises UnknownRef when there is none."""
    try:
        return run(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    except subprocess.CalledProcessError as error:
        raise UnknownRef(f"{ref!r} is not a branch, tag or commit in this repository") from error


def files_at(root: Path, commit: str) -> dict[str, str]:
    """Every file at a commit and its blob id, from `git ls-tree`. Submodules and symlinks are left out."""
    files: dict[str, str] = {}

    for entry in _split(run(root, "ls-tree", "-r", "-z", "--full-tree", commit)):
        details, path = entry.split("\t", 1)
        mode, kind, oid = details.split(" ")
        if kind == "blob" and mode.startswith("100"):
            files[path] = oid

    return files


def paths_containing(
    root: Path,
    commit: str,
    *,
    text: str,
    among: Collection[str],
) -> list[str]:
    """The text files among these paths that contain text at a commit, ignoring case."""
    if not among:
        return []

    # A path is given as ":(literal)path", so git doesn't read a "*" or "[" in it as a pattern.
    named = ["--", *(f":(literal){path}" for path in among)] if len(among) <= settings.GREP_PATHS else []
    try:
        output = run(root, "grep", "-l", "-z", "-I", "-i", "-F", "-e", text, commit, *named)
    except subprocess.CalledProcessError:
        # git grep exits with 1 when nothing matches.
        return []

    # git grep names each file "<commit>:<path>".
    found = (entry.removeprefix(commit + ":") for entry in _split(output))
    return [path for path in found if path in among]


def other_branches(root: Path) -> list[Branch]:
    """
    The default branch and every local and remote-tracking branch that isn't merged into it, leaving out the branch
    that is checked out. A merged branch holds nothing the default branch doesn't.
    """
    default = default_branch(root)
    if default is None:
        return []

    current = _optional(root, "symbolic-ref", "--quiet", "HEAD")
    fields = "%(refname)%00%(refname:short)%00%(objectname)%00%(committerdate:unix)%00%(symref)"
    listed = run(root, "for-each-ref", f"--format={fields}", f"--no-merged={default}", "refs/heads", "refs/remotes")
    listed += run(root, "for-each-ref", f"--format={fields}", default)

    found: dict[str, Branch] = {}
    for line in listed.decode("utf-8", "replace").splitlines():
        full_name, name, tip, date, symref = line.split("\0")
        # origin/HEAD is only another name for a branch that is listed already.
        if symref or full_name == current:
            continue
        remote = full_name.startswith("refs/remotes/")
        found[full_name] = Branch(name, tip, int(date or 0), remote, full_name == default)

    return list(found.values())


def default_branch(root: Path) -> str | None:
    """
    The full name of the branch that work merges into, such as "refs/remotes/origin/main". The remote's HEAD
    decides it when there is one. Without a remote, it is init.defaultBranch, main or master, whichever exists.
    """
    # origin/HEAD can name a branch the remote has since deleted or renamed, because fetch doesnt update it.
    remote_head = _optional(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if remote_head and _optional(root, "rev-parse", "--verify", "--quiet", remote_head):
        return remote_head

    configured = _optional(root, "config", "init.defaultBranch")
    for name in [configured, "main", "master"]:
        if name and _optional(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return f"refs/heads/{name}"

    return None


def merge_base(
    root: Path,
    *,
    first: str,
    second: str,
) -> str | None:
    """The newest commit that both commits have in their history, or None when they share none."""
    return _optional(root, "merge-base", first, second)


def changed_files(
    root: Path,
    *,
    base: str,
    tip: str,
) -> dict[str, str]:
    """The files that tip adds or changes compared with base, with their blob ids at tip. Deleted files are left out."""
    files: dict[str, str] = {}
    entries = _split(run(root, "diff-tree", "-r", "-z", "--no-renames", "--raw", "--no-abbrev", base, tip))

    # With -z, each file is two entries: ":<old mode> <new mode> <old blob> <new blob> <status>", then the path.
    for details, path in zip(entries[0::2], entries[1::2]):
        _, new_mode, _, new_oid, status = details.split(" ")
        if status != "D" and new_mode.startswith("100"):
            files[path] = new_oid

    return files


def last_fetch(root: Path) -> float | None:
    """When the repository last fetched from a remote, in seconds since 1970. None when it never has."""
    path = Path(run(root, "rev-parse", "--git-path", "FETCH_HEAD").decode().strip())
    try:
        return os.stat(path if path.is_absolute() else root / path).st_mtime
    except OSError:
        return None


def is_ancestor(root: Path, *, older: str, newer: str) -> bool:
    """True when the commit newer has the commit older somewhere in its history."""
    command = ["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer]
    return subprocess.run(command, capture_output=True, check=False).returncode == 0


def deletions(root: Path, since: str | None) -> list[Deletion]:
    """
    Every file deletion in HEAD's history, newest first. When since is given,
    only the commits after it are read.
    """
    revisions = f"{since}..HEAD" if since else "HEAD"

    log = run(
        root,
        "log",
        "--diff-filter=D",
        "--no-renames",
        "--raw",
        "--no-abbrev",
        "--diff-merges=first-parent",
        "--format=commit %H %cs",
        revisions,
    ).decode("utf-8", "replace")

    found: list[Deletion] = []
    commit = date = ""

    for line in log.splitlines():
        if line.startswith("commit "):
            _, commit, date = line.split(" ")
        elif line.startswith(":"):
            # A raw line reads ":100644 000000 <old blob> <new blob> D<tab>path".
            details, path = line.split("\t", 1)
            found.append(Deletion(path, details.split(" ")[2], commit, date))

    return found


def blob_sizes(root: Path, oids: list[str]) -> dict[str, int]:
    """The size in bytes of each blob git still has."""
    output = run(root, "cat-file", "--batch-check", stdin="\n".join(oids).encode() + b"\n").decode()

    # A line reads "<oid> blob <size>", or "<oid> missing" for a blob git has dropped.
    found = [line.split(" ") for line in output.splitlines()]
    return {fields[0]: int(fields[2]) for fields in found if len(fields) == 3}


def read_blobs(root: Path, oids: list[str]) -> Iterator[tuple[str, bytes | None]]:
    """
    Yield each blob's content in the order asked for, or None when git no longer has it. Several `git cat-file`
    commands run at once, each on settings.BLOBS_PER_CALL blobs, because one at a time they mostly wait on the disk.
    Only settings.GIT_THREADS outputs are held in memory at once.
    """
    chunks = [oids[start : start + settings.BLOBS_PER_CALL] for start in range(0, len(oids), settings.BLOBS_PER_CALL)]
    if len(chunks) == 1:
        yield from _blobs_in(chunks[0], _cat_file(root, chunks[0]))
        return

    def read(chunk: list[str]) -> bytes:
        return _cat_file(root, chunk)

    # Only a read of many blobs needs threads, so they are imported here, and a search doesn't pay for the import.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(settings.GIT_THREADS) as threads:
        for start in range(0, len(chunks), settings.GIT_THREADS):
            window = chunks[start : start + settings.GIT_THREADS]
            for chunk, output in zip(window, threads.map(read, window)):
                yield from _blobs_in(chunk, output)


def _cat_file(root: Path, oids: list[str]) -> bytes:
    return run(root, "cat-file", "--batch", stdin="\n".join(oids).encode() + b"\n")


def _blobs_in(oids: list[str], output: bytes) -> Iterator[tuple[str, bytes | None]]:
    """Each blob in the output of `git cat-file --batch`, which gives "<oid> blob <size>" and the content for each."""
    position = 0

    for oid in oids:
        header_end = output.index(b"\n", position)
        header = output[position:header_end].split(b" ")
        position = header_end + 1

        if header[-1] == b"missing":
            yield oid, None
            continue

        size = int(header[2])
        yield oid, output[position : position + size]
        position += size + 1


def commits_changing_text(root: Path, text: str, *, limit: int, timeout: float) -> list[str] | None:
    """
    One line per commit that added or removed text, newest first. This reads
    every commit's diff, so it returns None when it runs past the timeout.
    """
    try:
        log = run(root, "log", f"-S{text}", f"-n{limit}", "--format=%h %cs %s", "HEAD", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None

    return log.decode("utf-8", "replace").splitlines()


def blob_id(content: bytes) -> str:
    """The id git gives a file with this content in a SHA-1 repository."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def _command(root: Path, *args: str) -> list[str]:
    # With quotePath off, git prints non-ASCII paths as they are and not as escapes.
    return ["git", "-C", str(root), "-c", "core.quotePath=false", *args]


def _optional(root: Path, *args: str) -> str | None:
    """What a git command printed, or None when it failed or printed nothing."""
    try:
        return run(root, *args).decode().strip() or None
    except subprocess.CalledProcessError:
        return None


def _split(output: bytes) -> list[str]:
    return [entry for entry in output.decode("utf-8", "replace").split("\0") if entry]
