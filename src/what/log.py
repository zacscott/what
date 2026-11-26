"""
The log file every run of what writes to. It records each command, what the index read again and why a background
build failed, which nothing shows otherwise.
"""

import logging
import os
from pathlib import Path
from typing import Final, override

from what import streams
from what.config import settings

# The logger every module's own logger passes its records to.
ROOT: Final = "what"

# The key of a log record that holds the text to print to stderr, when that is not "what: " and the message.
SHOWN_AS: Final = "shown_as"


class ErrorsToStderr(logging.Handler):
    """
    Prints every log record at ERROR or above to stderr, so an error is never only in the log file. A record is
    printed as "what: " and its message, with the traceback when it has one.
    """

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.setFormatter(logging.Formatter("what: %(message)s"))

    @override
    def emit(self, record: logging.LogRecord) -> None:
        shown: str | None = getattr(record, SHOWN_AS, None)
        streams.err(self.format(record) if shown is None else shown)


def path() -> Path:
    """
    Where the log file is: $XDG_STATE_HOME/what/what.log, or ~/.local/state/what/what.log when that isn't set.
    It is not in the cache folder, because deleting the cache must not delete the log.
    """
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "what" / "what.log"


def show_errors() -> None:
    """Print every record at ERROR or above to stderr from now on. Calling it again changes nothing."""
    logger = logging.getLogger(ROOT)
    if not any(isinstance(handler, ErrorsToStderr) for handler in logger.handlers):
        logger.addHandler(ErrorsToStderr())


def start() -> None:
    """
    Send every record at INFO or above to the log file, and every error to stderr as well. When the file can't
    be written, as in a sandbox with a read-only home folder, nothing is logged and the command runs as usual.
    """
    # A log line that can't be written, as on a full disk, is dropped. Python would print a traceback for each one.
    logging.raiseExceptions = False
    show_errors()

    logger = logging.getLogger(ROOT)
    if any(isinstance(handler, logging.FileHandler) for handler in logger.handlers):
        return

    file = path()
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        _rotate(file)
        handler = logging.FileHandler(file, encoding="utf-8")
    except OSError:
        return

    # The process id tells apart the lines of a search and of a background build that run at the same time.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(process)d %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _rotate(file: Path) -> None:
    """
    Move a log file that has reached settings.LOG_MAX_BYTES to "what.log.1", replacing the one before it. A
    process that has the old file open keeps writing to it until it ends.
    """
    try:
        too_big = file.stat().st_size >= settings.LOG_MAX_BYTES
    except FileNotFoundError:
        return

    if not too_big:
        return

    try:
        os.replace(file, file.with_name(file.name + ".1"))
    except FileNotFoundError:
        # Another process that started at the same time has moved the file already.
        pass
