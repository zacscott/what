"""Turns the text or regex a search was given into a pattern, with the literal text every match must hold."""

import re
from enum import Enum
from typing import Final, NamedTuple

# The flags a regex can start with to set them for the whole pattern, such as "(?i)" or "(?ms)".
GLOBAL_FLAGS: Final = re.compile(r"(?:\(\?[aiLmsux]+\))+")


class InvalidPattern(ValueError):
    """Raised when the pattern given to grep with --regex is not a regex Python can compile."""


class Case(Enum):
    """How a text search treats capital letters."""

    # Ignore case unless the pattern has a capital letter in it.
    SMART = "smart"
    IGNORE = "ignore"
    SENSITIVE = "sensitive"


class Matcher(NamedTuple):
    """A compiled search pattern, and the plain text every match must contain."""

    pattern: re.Pattern[str]
    literals: list[str]


def matcher_for(
    pattern: str,
    *,
    regex: bool,
    case: Case = Case.SMART,
    word: bool = False,
) -> Matcher:
    """
    The compiled pattern, and the plain text every match must contain, for the trigram index to look up. With word,
    a match must not have a letter, digit or underscore straight before or after it. Raises InvalidPattern for a
    regex that doesn't compile.
    """
    # An escape like \S is not a capital letter.
    letters = re.sub(r"\\.", "", pattern) if regex else pattern
    has_capital = any(letter.isupper() for letter in letters)
    sensitive = case == Case.SENSITIVE or (case == Case.SMART and has_capital)

    source = pattern if regex else re.escape(pattern)
    try:
        # The pattern is checked as it was typed, because the text that -w adds around it can hide a mistake.
        re.compile(source)
    except re.error as error:
        raise InvalidPattern(f"{pattern!r} is not a regex Python can read: {error}") from error

    if word:
        # Flags for the whole pattern, such as "(?i)", must stay at its start.
        flags = GLOBAL_FLAGS.match(source)
        start = flags.end() if flags else 0
        # With the x flag a pattern can end in a comment, which runs to the end of its line.
        end = "\n" if "x" in source[:start] else ""
        source = rf"{source[:start]}(?<!\w)(?:{source[start:]}{end})(?!\w)"

    try:
        matcher = re.compile(source, 0 if sensitive else re.IGNORECASE)
    except re.error as error:
        raise InvalidPattern(f"{pattern!r} is not a regex Python can read: {error}") from error

    literals = required_literals(pattern) if regex else [pattern] if len(pattern) >= 3 else []
    return Matcher(matcher, literals)


def required_literals(pattern: str) -> list[str]:
    """
    The runs of 3 or more plain characters that every match of a regex must
    contain. The trigram index can only look up plain text, so these pick the
    candidate files. An empty list means every file has to be read. For example:

        required_literals(r"class \\w+Controller") == ["class ", "Controller"]
    """
    # With a group or an alternative, a run of text may be optional, so nothing is treated as required.
    if "|" in pattern or "(" in pattern:
        return []

    runs: list[str] = []
    run = ""
    position = 0

    while position < len(pattern):
        character = pattern[position]
        if character == "\\" and position + 1 < len(pattern):
            escaped = pattern[position + 1]
            # These escapes spell one character as a code (\x41, \101, \u00e9). The digits after them are not text.
            if escaped in "xuUN" or escaped.isdigit():
                return []
            # Any other escaped letter is a class like \w. An escaped symbol is that symbol itself.
            if escaped.isalnum():
                runs.append(run)
                run = ""
            else:
                run += escaped
            position += 2
            continue

        if character in "?*{":
            # The character before a "?", "*" or "{" may be absent from a match.
            runs.append(run[:-1])
            run = ""
            if character == "{":
                position = _index_or_end(pattern, "}", start=position)
        elif character == "[":
            # In a class like "[]a]" or "[\]a]" the first "]" does not end the class. We don't work out where it ends.
            closing = _index_or_end(pattern, "]", start=position + 1)
            if pattern.startswith(("]", "^]"), position + 1) or "\\" in pattern[position:closing]:
                return []
            runs.append(run)
            run = ""
            position = closing
        elif character in ".+^$)]}":
            runs.append(run)
            run = ""
        else:
            run += character
        position += 1

    runs.append(run)
    return [run for run in runs if len(run) >= 3]


def _index_or_end(pattern: str, character: str, *, start: int) -> int:
    found = pattern.find(character, start)
    return found if found != -1 else len(pattern)
