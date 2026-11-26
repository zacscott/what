"""
Writes to stdout and stderr for the command line. Every write goes through one function. That function handles
a stream that was closed before the command started. It also handles a reader, such as head, that has stopped
reading.
"""

import os
import select
import sys
from typing import TextIO


class ReaderGone(Exception):
    """
    Raised when the reader of the output, such as head, has stopped reading. click would end the process with 1
    for this, which means "nothing found", so it is told apart before click sees it.
    """


def out(text: str) -> None:
    """
    Print a line, or several, on stdout. When stdout can't take the text, as on a full disk, this raises OSError,
    because output that is lost is a failure of the command.
    """
    _write(sys.stdout, text + "\n")


def err(text: str, *, end: str = "\n") -> None:
    """Print a line, or several, on stderr. end is what follows the text, which is nothing for a progress line."""
    try:
        _write(sys.stderr, text + end)
    except OSError:
        # A message that stderr can't take has nowhere else to go, and the log file still has every error.
        pass


def flush() -> None:
    """Write out what click printed by itself, such as the help. A reader that has gone is handled as in a write."""
    for stream in (sys.stdout, sys.stderr):
        try:
            _write(stream, "")
        except OSError:
            # Python reports what it can't flush as it exits, and exits with 120.
            pass


def _write(stream: TextIO | None, text: str) -> None:
    """
    Write text to a stream and flush it. Python sets a stream to None when the command starts with it closed, and
    the text is then dropped. When the reader has gone, the rest of the output is sent nowhere. Any other
    failure, such as a full disk, raises OSError.
    """
    if stream is None:
        return

    try:
        stream.write(text)
        stream.flush()
    except BrokenPipeError:
        stop_writing()


def stop_writing() -> None:
    """
    Send the rest of the output nowhere, after the reader, such as head, stopped reading. Without this, Python
    would print a traceback and exit with 120 when it flushes what is left as it exits.
    """
    nowhere = os.open(os.devnull, os.O_WRONLY)
    for stream in (sys.stdout, sys.stderr):
        # Python sets a stream to None when the command starts with it closed.
        if stream:
            os.dup2(nowhere, stream.fileno())


def reader_has_gone() -> bool:
    """True when stdout or stderr is a pipe that nothing reads any more."""
    watched = select.poll()
    for stream in (sys.stdout, sys.stderr):
        if stream:
            # Nothing is asked for, because poll always reports a pipe whose other end has closed.
            watched.register(stream.fileno(), 0)

    return any(event & (select.POLLERR | select.POLLHUP) for _, event in watched.poll(0))


def broken_pipe() -> Exception:
    """
    What to raise in place of a broken pipe. click would end the process with 1 for one, with no message. When
    the output's reader has gone, this returns ReaderGone. A pipe that anything else broke is a failure of the
    command. This returns an error for it that click passes on, and the caller logs that error.
    """
    if reader_has_gone():
        return ReaderGone()

    return RuntimeError("a pipe that the command wrote to was closed, and it was not the output")
