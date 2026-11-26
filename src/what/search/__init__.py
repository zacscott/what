"""
The searches the command line offers. Each one returns a Result: the lines it found, which output prints as plain
text, in colour or as JSON, and whether it found anything.
"""

from what.search.branches import BRANCHES_BUILDING
from what.search.callers import callers
from what.search.files import outline, show
from what.search.lines import CUT_BEFORE
from what.search.lookup import lookup
from what.search.names import PARTIAL_MATCHES, find
from what.search.patterns import Case, InvalidPattern, required_literals
from what.search.text import HISTORY_BUILDING, Listing, grep

__all__ = [
    "BRANCHES_BUILDING",
    "CUT_BEFORE",
    "HISTORY_BUILDING",
    "PARTIAL_MATCHES",
    "Case",
    "InvalidPattern",
    "Listing",
    "callers",
    "find",
    "grep",
    "lookup",
    "outline",
    "required_literals",
    "show",
]
