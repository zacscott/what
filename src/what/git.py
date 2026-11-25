"""The git commands the index is built from, each as one small function."""

import hashlib
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from what.config import settings


class NotARepository(Exception):
    """The folder the command ran in is not inside a git repository."""


class UnknownRef(Exception):
    """A ref the user named is not a branch, tag or commit in the repository."""


@dataclass(frozen=True)
class Deletion:
    """A file that a commit deleted, and the blob that held its last content."""

    path: str
    oid: str
    commit: str
    date: str


def run(root: Path, *args: str, stdin: bytes | None = None, timeout: float | None = None) -> bytes:
    """Run a git command in the repository and return what it printed. A failed command raises."""
    return subprocess.run(_command(root, *args), input=stdin, capture_output=True, check=True, timeout=timeout).stdout


def repo_root(start: Path) -> Path:
    try:
        return Path(run(start, "rev-parse", "--show-toplevel").decode().strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise NotARepository(f"{start} is not inside a git repository") from error


def head(root: Path) -> str | None:
    """The commit HEAD points at, or None in a repository with no commits."""
    try:
        return run(root, "rev-parse", "--quiet", "--verify", "HEAD").decode().strip()
    except subprocess.CalledProcessError:
        return None


def working_tree(root: Path) -> tuple[dict[str, str], set[str], list[str]]:
    """
    The tracked files with their blob ids, the tracked files that changed, and the untracked files. The three git
    commands run at the same time, because each one takes 10 to 30ms and none of them depends on another.
    """
    commands = [
        ["ls-files", "--stage", "-z"],
        ["diff-files", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ]
    running = [
        subprocess.Popen(_command(root, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for args in commands
    ]

    outputs: list[bytes] = []
    for process in running:
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
        outputs.append(stdout)

    staged, changed, untracked = outputs
    return _tracked(staged), set(_split(changed)), _split(untracked)


def _tracked(output: bytes) -> dict[str, str]:
    """Each tracked file's path and the blob id git's index holds for it, from `git ls-files --stage -z`."""
    files: dict[str, str] = {}

    for entry in _split(output):
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


def paths_containing(root: Path, commit: str, text: str) -> list[str]:
    """The text files at a commit tjat contain text, ignoring case."""
    try:
        output = run(root, "grep", "-l", "-z", "-I", "-i", "-F", "-e", text, commit)
    except subprocess.CalledProcessError:
        # git grep exits with 1 when nothing matches.
        return []

    # git grep names each file "<commit>:<path>".
    return [entry.removeprefix(commit + ":") for entry in _split(output)]


def is_ancestor(root: Path, older: str, newer: str) -> bool:
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
    """Yield each blob's content in the order asked for, or None when git no longer has it."""
    for start in range(0, len(oids), settings.BLOBS_PER_CALL):
        chunk = oids[start : start + settings.BLOBS_PER_CALL]
        output = run(root, "cat-file", "--batch", stdin="\n".join(chunk).encode() + b"\n")
        position = 0

        for oid in chunk:
            header_end = output.index(b"\n", position)
            header = output[position:header_end].split(b" ")
            position = header_end + 1

            if header[-1] == b"missing":
                yield oid, None
                continue

            size = int(header[2])
            yield oid, output[position : position + size]
            position += size + 1


def commits_changing_text(root: Path, text: str, limit: int, timeout: float) -> list[str] | None:
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


def _split(output: bytes) -> list[str]:
    return [entry for entry in output.decode("utf-8", "replace").split("\0") if entry]
