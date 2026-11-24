"""
The searches the command line offers. Each one returns the text the model reads:
a path printed once, with its matches under it as "line: text".
"""

import re
import sqlite3
from collections.abc import Iterator
from functools import cache

from what import git
from what.config import settings
from what.index import Index
from what.models import Place, SymbolHit, path_matches
from what.symbols import split_lines

TYPE_KINDS = {"class", "interface", "trait", "enum", "struct", "type", "module", "namespace", "impl"}
CALLABLE_KINDS = {"function", "method"}

# The words of a name or path, split at case changes, digits and punctuation. For example
# "getHTTPRequest_v2" gives get, HTTP, Request, v and 2.
NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

# A file and a line or a range of lines, such as "src/a.py:45" or "src/a.py:45-80".
LINE_RANGE = re.compile(r"(.+?):(\d+)(?:-(\d+))?")

# A deleted file as output names it, such as "a1b2c3d4^:legacy/split.py".
DELETED_REF = re.compile(r"([0-9a-f]{4,40})\^:(.+)")

HISTORY_BUILDING = "(The index of deleted files is not up to date, so deleted files may be missing.)"


def lookup(
    index: Index,
    query: str,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 30,
) -> str:
    """
    Run the search that fits the query. A line range such as "src/a.py:45-80" is shown. A query that
    names one file gives the file's outline, or its text when it defines nothing. Anything else is a name for find.
    """
    index.refresh()

    if LINE_RANGE.fullmatch(query):
        return _show(index, query, path)

    # A plain word is always a name. Scripts like bin/refresh are often named after a function, and the function
    # is what a search for "refresh" wants.
    names_a_file = not kind and ("/" in query or "." in query)
    with index.connect() as db:
        places = _resolve_path(db, query) if names_a_file else []
        defines_something = len(places) == 1 and bool(SymbolHit.in_blob(db, places[0].blob_row))

    if len(places) != 1:
        return _find(index, query, kind, path, limit)
    return _outline(index, query) if defines_something else _show(index, query, None)


def find(
    index: Index,
    query: str,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 30,
) -> str:
    """Symbols and file paths whose name matches the query."""
    index.refresh()
    return _find(index, query, kind, path, limit)


def _find(index: Index, query: str, kind: str | None, path: str | None, limit: int) -> str:
    words = query.split()
    if not words:
        return "Give a symbol name or part of a file path."

    parent, name = _split_qualified("".join(words))

    with index.connect() as db:
        hits = _find_symbols(db, name, words, parent, kind, path)
        paths = [] if kind else _find_paths(db, words, path)
        if not hits and not paths:
            hits, paths = _find_abbreviated(db, name, parent, kind, path)

    if not hits and kind:
        return f"No {kind} named {query!r}. Run find without kind to see other matches." + _history_note(index)
    if not hits and not paths:
        # A grammar can miss a definition, so the text of the files is searched before saying "nothing".
        text_matches = _text_search(index, query, False, path, limit, False)
        if not text_matches:
            return f"Nothing named or containing {query!r} in live or deleted files." + _history_note(index)
        return "\n".join([f"No symbol or path named {query!r}. Text matches:", *text_matches]) + _history_note(index)

    output: list[str] = []
    shown = hits[:limit]
    by_place: dict[Place, list[SymbolHit]] = {}
    for hit in shown:
        by_place.setdefault(hit.place, []).append(hit)

    for place, group in by_place.items():
        output.extend(f"{place.ref}:{hit.line}:{hit.signature}" for hit in sorted(group, key=lambda hit: hit.line))
    listed = {place.path for place in by_place}
    output.extend(place.ref for place in paths[:10] if place.path not in listed)

    if len(hits) > limit:
        more_files = len({hit.place for hit in hits[limit:]} - set(by_place))
        output.append(f"+{len(hits) - limit} more symbols in {more_files} other files. Add path or kind to narrow.")

    return "\n".join(output) + _history_note(index)


def grep(
    index: Index,
    pattern: str,
    regex: bool = False,
    path: str | None = None,
    limit: int = 40,
    deleted: bool = False,
    commits: bool = False,
) -> str:
    """
    Lines containing the pattern, each under the symbol it sits in. Deleted
    files are searched when asked for, or when no live file matches.
    """
    index.refresh()

    try:
        output = _text_search(index, pattern, regex, path, limit, deleted)
    except re.error as error:
        return f"Invalid regex: {error}"

    if commits:
        output.extend(_commits_changing(index, pattern))

    if not output:
        return f"No text matches for {pattern!r} in live or deleted files." + _history_note(index)
    return "\n".join(output) + _history_note(index)


def _text_search(
    index: Index,
    pattern: str,
    regex: bool,
    path: str | None,
    limit: int,
    deleted: bool,
) -> list[str]:
    """The output lines of a text search, empty when nothing matches."""
    # The search ignores case unless the pattern has a capital letter in it. An escape like \S is not a capital.
    letters = re.sub(r"\\.", "", pattern) if regex else pattern
    flags = 0 if any(letter.isupper() for letter in letters) else re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    literals = required_literals(pattern) if regex else [pattern] if len(pattern) >= 3 else []

    with index.connect() as db:
        live = [place for place in Place.live_with_text(db, literals) if path_matches(place.path, path)]
        texts = ((place, index.read_live(place.path)) for place in live)

        output = _matching_lines(db, texts, matcher, limit, len(live))
        if output and not deleted:
            return output

        if not literals:
            if output:
                output.append("Deleted files need a pattern with 3 or more plain characters in a row.")
            return output

        gone = [place for place in Place.deleted_with_text(db, literals) if path_matches(place.path, path)]
        blobs = git.read_blobs(index.root, [place.oid for place in gone[: settings.DELETED_FILES_READ]])

        deleted_texts = (
            (place, content.decode("utf-8", "replace") if content else None) for place, (_, content) in zip(gone, blobs)
        )

        output.extend(_matching_lines(db, deleted_texts, matcher, limit, len(gone)))

    return output


def outline(index: Index, path: str) -> str:
    """Every symbol in one file with its line range, indented by nesting."""
    index.refresh()
    return _outline(index, path)


def _outline(index: Index, path: str) -> str:
    with index.connect() as db:
        places = _resolve_path(db, path)
        if len(places) != 1:
            return _path_choices(path, places)
        symbols = SymbolHit.in_blob(db, places[0].blob_row)

    if not symbols:
        return f"{places[0].ref}\n  (no symbols found; use show to read it)"

    output = [places[0].ref]
    open_ends: list[int] = []
    for hit in symbols:
        while open_ends and hit.line > open_ends[-1]:
            open_ends.pop()
        output.append(f"{'  ' * (len(open_ends) + 1)}{hit.line}-{hit.end_line} {hit.signature}")
        open_ends.append(hit.end_line)

    return "\n".join(output)


def show(index: Index, target: str, path: str | None = None) -> str:
    """
    The source of one symbol, a line range or a whole file. target is a symbol
    name ("bandana", "BananaBender.bandana"), "path:45-80" or a path. Deleted
    files are read from git.
    """
    index.refresh()
    return _show(index, target, path)


def _show(index: Index, target: str, path: str | None) -> str:
    with index.connect() as db:
        line_range = LINE_RANGE.fullmatch(target)
        places = _resolve_path(db, line_range.group(1) if line_range else target)

        if line_range and len(places) != 1:
            return _path_choices(line_range.group(1), places)

        if len(places) == 1:
            first = int(line_range.group(2)) if line_range else 1
            last = int(line_range.group(3) or first) if line_range else None
            return _source(index, places[0], first, last)

        parent, name = _split_qualified(target)
        hits = [hit for hit in _find_symbols(db, name, [name], parent, None, path) if hit.name.lower() == name.lower()]

    if not hits:
        if len(places) > 1:
            return _path_choices(target, places)
        return f"No symbol or file called {target!r}. Use find to search by part of a name."

    best = hits[0]
    output = _source(index, best.place, best.line, best.end_line)

    if len(hits) > 1:
        others = [f"  {hit.place.ref}:{hit.line}" for hit in hits[1:6]]
        output += "\nAlso defined at (pass path to pick one):\n" + "\n".join(others)

    return output


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
                position = _index_or_end(pattern, "}", position)
        elif character == "[":
            # In a class like "[]a]" or "[\]a]" the first "]" does not end the class. We don't work out where it ends.
            closing = _index_or_end(pattern, "]", position + 1)
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


def _index_or_end(pattern: str, character: str, start: int) -> int:
    found = pattern.find(character, start)
    return found if found != -1 else len(pattern)


def _split_qualified(name: str) -> tuple[str | None, str]:
    """Split "Parent::name", "Parent.name" or "Parent#name" into its two parts."""
    parts = re.split(r"::|[.#]", name)
    if len(parts) >= 2 and all(parts[-2:]):
        return parts[-2], parts[-1]
    return None, name


def _find_symbols(
    db: sqlite3.Connection,
    name: str,
    words: list[str],
    parent: str | None,
    kind: str | None,
    path: str | None,
) -> list[SymbolHit]:
    """Symbols matching a name, best first: exact, then prefix, then containing every word."""
    return _ranked(
        [
            SymbolHit.named(db, name, parent, kind, path),
            SymbolHit.starting_with(db, name, parent, kind, path),
            SymbolHit.containing_all(db, words, parent, kind, path),
        ]
    )


def _find_abbreviated(
    db: sqlite3.Connection,
    name: str,
    parent: str | None,
    kind: str | None,
    path: str | None,
) -> tuple[list[SymbolHit], list[Place]]:
    """
    Symbols and paths that match the query as an abbreviation, for when nothing matches it more closely.
    "reqcontroller" finds "RequestController".
    """
    letters = "".join(NAME_WORDS.findall(name)).lower()
    if len(letters) < 2:
        return [], []

    symbols = SymbolHit.containing_in_order(db, list(letters), parent, kind, path)
    hits = _ranked([[hit for hit in symbols if _is_abbreviation(letters, hit.name)]])

    if kind:
        return hits, []

    places = Place.path_containing_in_order(db, list(letters))
    paths = [place for place in places if _is_abbreviation(letters, place.path) and path_matches(place.path, path)]
    return hits, sorted(paths, key=lambda place: (place.commit is not None, len(place.path)))


def _is_abbreviation(letters: str, text: str) -> bool:
    """
    True when the query can be cut into pieces that each start a word of text, in order, with any words
    between. Case is ignored, so "reqcontroller" fits "getRequestController" as req and controller. It
    doesn't fit "frequencyController".
    """
    words = [word.lower() for word in NAME_WORDS.findall(text)]

    @cache
    def fits(start: int, word: int) -> bool:
        """Whether letters from start onward can be cut into word starts, using words from word onward."""
        if start == len(letters):
            return True

        for index in range(word, len(words)):
            shared = 0
            while start + shared < len(letters) and shared < len(words[index]):
                if letters[start + shared] != words[index][shared]:
                    break
                shared += 1
            if any(fits(start + length, index + 1) for length in range(shared, 0, -1)):
                return True
        return False

    return fits(0, 0)


def _ranked(tiers: list[list[SymbolHit]]) -> list[SymbolHit]:
    """
    Ran ksymbol hits best first. A hit from an earlier tier ranks first, and a hit that is in several tiers
    keeps its earliest.
    """
    ranked: dict[tuple[str, str, int], tuple[tuple[int, bool, int, bool, int], SymbolHit]] = {}
    for tier, hits in enumerate(tiers):
        for hit in hits:
            key = (hit.place.path, hit.name, hit.line)
            if key in ranked:
                continue
            kind_rank = 0 if hit.kind in TYPE_KINDS else 1 if hit.kind in CALLABLE_KINDS else 2
            is_deleted = hit.place.commit is not None
            rank = (tier, is_deleted, kind_rank, _is_secondary(hit.place.path), len(hit.place.path))
            ranked[key] = (rank, hit)

    return [hit for _, hit in sorted(ranked.values(), key=lambda entry: entry[0])]


def _find_paths(db: sqlite3.Connection, words: list[str], path_filter: str | None) -> list[Place]:
    """Live then deleted files whose path contains every word, shortest path first."""
    return sorted(
        (place for place in Place.path_containing_all(db, words) if path_matches(place.path, path_filter)),
        key=lambda place: (place.commit is not None, len(place.path)),
    )


def _resolve_path(db: sqlite3.Connection, path: str) -> list[Place]:
    """The file a path names: an exact match if there is one, or else every file whose path ends with it."""
    deleted = DELETED_REF.fullmatch(path)
    if deleted:
        commit, path = deleted.groups()
        return [place for place in _resolve_path(db, path) if place.commit and place.commit.startswith(commit)]

    path = path.removeprefix("./")
    exact = Place.at_path(db, path)
    if exact:
        return exact[:1]
    return Place.ending_with(db, path)


def _path_choices(path: str, places: list[Place]) -> str:
    if not places:
        return f"No file matches {path!r}. Use find to search for it."
    return f"{path!r} matches several files:\n" + "\n".join(f"  {place.ref}" for place in places[:20])


def _matching_lines(
    db: sqlite3.Connection,
    texts: Iterator[tuple[Place, str | None]],
    matcher: re.Pattern[str],
    limit: int,
    candidates: int,
) -> list[str]:
    """
    The output lines for the candidate files that really match, stopping once
    limit matching lines are shown. candidates is how many files texts holds.
    """
    output: list[str] = []
    shown = 0
    read = 0

    for place, text in texts:
        if shown >= limit:
            break
        read += 1
        if text is None:
            continue

        matches = [(number, line) for number, line in enumerate(split_lines(text), 1) if matcher.search(line)]
        if not matches:
            continue

        output.extend(
            _lines_under_symbols(place.ref, SymbolHit.in_blob(db, place.blob_row), matches[: settings.LINES_PER_FILE])
        )
        shown += min(len(matches), settings.LINES_PER_FILE)
        if len(matches) > settings.LINES_PER_FILE:
            output.append(f"({len(matches) - settings.LINES_PER_FILE} more matching lines in {place.ref})")

    if candidates > read:
        output.append(f"+{candidates - read} more files may match. Add path or a longer pattern to narrow.")
    return output


def _lines_under_symbols(
    ref: str,
    symbols: list[SymbolHit],
    matches: list[tuple[int, str]],
) -> list[str]:
    """
    One file's matching lines as "path:line:text". Where the enclosing symbol changes, its first line comes before
    them as a context line, "path-line-text", the way grep and ripgrep print context.
    """
    output: list[str] = []
    last_symbol: SymbolHit | None = None

    for number, line in matches:
        symbol = _innermost(symbols, number)
        if symbol is not None and symbol != last_symbol and symbol.line != number:
            output.append(f"{ref}-{symbol.line}-{symbol.signature}")
        last_symbol = symbol
        output.append(f"{ref}:{number}:{line.strip()[: settings.MAX_LINE_LENGTH]}")

    return output


def _innermost(symbols: list[SymbolHit], line: int) -> SymbolHit | None:
    around = [symbol for symbol in symbols if symbol.line <= line <= symbol.end_line]
    return min(around, key=lambda symbol: symbol.end_line - symbol.line) if around else None


def _source(index: Index, place: Place, first: int, last: int | None) -> str:
    """Lines first to last of a file, under a "path:first-last" heading. last=None means to the end."""
    if place.commit is None:
        text = index.read_live(place.path)
    else:
        content = next(iter(git.read_blobs(index.root, [place.oid])))[1]
        text = content.decode("utf-8", "replace") if content else None

    if text is None:
        return f"{place.ref} can't be shown: it is binary, larger than 1MB, or no longer in git."

    lines = split_lines(text)
    last = min(last or len(lines), len(lines))
    shown_last = min(last, first + settings.SHOW_MAX_LINES - 1)
    output = [f"{place.ref}:{first}-{shown_last}", *lines[first - 1 : shown_last]]

    if shown_last < last:
        output.append(f"({last - shown_last} more lines. Continue with show {place.ref}:{shown_last + 1}-{last})")

    return "\n".join(output)


def _commits_changing(index: Index, text: str) -> list[str]:
    if not index.in_git:
        return ["Commit search needs a git repository."]

    commits = git.commits_changing_text(index.root, text, limit=10, timeout=settings.COMMIT_SEARCH_SECONDS)

    if commits is None:
        return [f"The commit search ran past {settings.COMMIT_SEARCH_SECONDS} seconds and was stopped."]
    if not commits:
        return [f"No commit added or removed {text!r}."]
    return [f"Commits that added or removed {text!r}:", *(f"  {commit}" for commit in commits)]


def _history_note(index: Index) -> str:
    return "" if index.history_is_current() else "\n" + HISTORY_BUILDING


def _is_secondary(path: str) -> bool:
    """Tests and third-party code rank below the code a search is most likely after."""
    lowered = path.lower()
    return any(part in lowered for part in ("test", "spec", "vendor/", "node_modules/", "fixture"))
