"""Searches a git repository by symbol and text, deleted files included."""

import logging

# Without a handler, Python prints warnings to stderr. Records go only to the log file that log.start sets up.
logging.getLogger(__name__).addHandler(logging.NullHandler())
