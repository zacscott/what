"""
The searches the command line offers. Each one returns the text the model reads:
a path printed once, with its matches under it as "line: text".
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from typing import assert_never

from what import dependencies, git
from what.config import settings
from what.dependencies import DependencyReach
from what.index import Index
from what.models import (
    AtRef,
    Deleted,
    IndexSession,
    InPackage,
    Live,
    OnBranch,
    Place,
    Scope,
    SymbolHit,
    age,
    path_matches,
)
from what.symbols import Symbol, extract, split_lines, text_roles

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
BRANCHES_BUILDING = "(The index of other branches is not up to date, so {count} branches may be missing.)"

# Ends a grep hit on the line that definse the symbol, so the definition stands out from the call sites.
DEFINITION_MARK = "  [definition]"

# Ends a callers hit that may not call the symbol, such as a method called on a variable whose class isn't known.
POSSIBLE_MARK = "  [possible]"

# What comes before a name on its line when something holds it: the holder and the operator, such as "$this->",
# "Deepgram::", "self." or a namespace like "App\".
RECEIVER = re.compile(r"([\w$\\]*)\s*(::|\?->|->|\?\.|\.|\\)\s*$")


def lookup(
    index: Index,
    query: str,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 30,
    ref: str | None = None,
    branches: bool = False,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> str:
    """
    Run the search that fits the query. A line range such as "src/a.py:45-80" is shown. A query that
    names one file gives the file's outline, or its text when it defines nothing. Anything else is a name for find.
    """
    if ref is not None:
        return _lookup_at_ref(index, query, kind, path, limit, ref)

    index.refresh()

    if LINE_RANGE.fullmatch(query):
        return _show(index, query, path, deps)

    # A plain word is always a name. Scripts like bin/refresh are often named after a function, and the function
    # is what a search for "refresh" wants.
    names_a_file = not kind and ("/" in query or "." in query)
    with index.connect() as session:
        places = _resolve_path(session, query) if names_a_file else []
        defines_something = len(places) == 1 and bool(session.symbols_in_blob(places[0].blob_row))

    if len(places) != 1:
        return _find(index, query, kind, path, limit, branches, deps)
    return _outline(index, query) if defines_something else _show(index, query, None, deps)


def _lookup_at_ref(
    index: Index,
    query: str,
    kind: str | None,
    path: str | None,
    limit: int,
    ref: str,
) -> str:
    if LINE_RANGE.fullmatch(query):
        return _show_at_ref(index, query, path, ref)

    names_a_file = not kind and ("/" in query or "." in query)
    places = _resolve_tree_path(_tree_at(index, ref)[1], ref, query) if names_a_file else []

    if len(places) != 1:
        return _find_at_ref(index, query, kind, path, limit, ref)
    return _outline_at_ref(index, query, ref)


def find(
    index: Index,
    query: str,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 30,
    ref: str | None = None,
    branches: bool = False,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> str:
    """
    Symbols and file paths whose name matches the query. With a ref, the search reads the files at that ref from git
    in place of the working tree. Other branches are searched as well when branches is True, or when the working
    tree has no match. deps says when the installed dependencies are searched.
    """
    if ref is not None:
        return _find_at_ref(index, query, kind, path, limit, ref)

    index.refresh()
    return _find(index, query, kind, path, limit, branches, deps)


def _find(
    index: Index,
    query: str,
    kind: str | None,
    path: str | None,
    limit: int,
    branches: bool,
    deps: DependencyReach,
) -> str:
    words = query.split()
    if not words:
        return "Give a symbol name or part of a file path."

    parent, name = _split_qualified("".join(words))

    in_dependencies = _in_dependencies(index)
    with index.connect() as session:
        hits = _find_symbols(session, name, words, parent, kind, path)
        paths = [] if kind else _find_paths(session, words, path)
        if not hits and not paths:
            hits, paths = _find_abbreviated(session, name, parent, kind, path)

    hits = [hit for hit in hits if not in_dependencies(hit.place.path)]
    paths = [place for place in paths if not in_dependencies(place.path)]

    here = _found_lines(hits, paths, limit) if hits or paths else []
    elsewhere: list[str] = []
    notes: list[str] = []
    if branches or not here:
        elsewhere, notes = _find_on_branches(index, name, words, parent, kind, path, limit, bool(here))

    if deps != DependencyReach.FALLBACK or not here:
        package_hits, package_notes = _dependency_symbols(index, name, words, parent, kind, deps)
        elsewhere += _found_lines(_ranked(package_hits), [], limit)
        notes += package_notes

    if here or elsewhere:
        return "\n".join(here + elsewhere + notes)

    if kind:
        return "\n".join([f"No {kind} named {query!r}. Run find without kind to see other matches.", *notes])

    # A grammar can miss a definition, so the text of the files is searched before saying "nothing".
    text_matches = _text_search(index, query, False, path, limit, False).lines
    if not text_matches:
        nothing = f"Nothing named or containing {query!r} in live or deleted files, other branches or dependencies."
        return "\n".join([nothing, *notes])
    return "\n".join([f"No symbol or path named {query!r}. Text matches:", *text_matches, *notes])


def _dependency_symbols(
    index: Index,
    name: str,
    words: list[str],
    parent: str | None,
    kind: str | None,
    deps: DependencyReach,
) -> tuple[list[list[SymbolHit]], list[str]]:
    """
    Symbols in the installed dependencies that match a name, in the tiers find ranks, and a note for each lockfile
    with packages that aren't installed.
    """
    installed = dependencies.installed(index, deps == DependencyReach.ALL)
    packages = dependencies.package_rows(index, installed.packages)
    if not packages:
        return [], list(installed.notes)

    return dependencies.find_symbols(index, packages, name, words, parent, kind), list(installed.notes)


def _find_on_branches(
    index: Index,
    name: str,
    words: list[str],
    parent: str | None,
    kind: str | None,
    path: str | None,
    limit: int,
    found_here: bool,
) -> tuple[list[str], list[str]]:
    """
    The output of find on other branches, empty when no branch has a match that the working tree lacks, and a note
    when some branches aren't indexed yet.
    """
    branches, notes = _read_branches(index)
    if not branches:
        return [], notes

    with index.connect() as session:
        hits = _find_symbols(session, name, words, parent, kind, path, Scope.BRANCHES)
        paths = [] if kind else _find_paths(session, words, path, Scope.BRANCHES)

    if not hits and not paths:
        return [], notes

    places = [hit.place for hit in hits[:limit]] + paths
    found = [_branches_heading(places, found_here), *_found_lines(hits, paths, limit), *_fetch_note(index, branches)]
    return found, notes


def _read_branches(index: Index) -> tuple[list[git.Branch], list[str]]:
    """
    The other branches, after reading the ones that moved. When too many moved for a search to wait on, a process
    in the background reads them, and the note says some branches may be missing.
    """
    branches, pending = index.refresh_branches(settings.BRANCHES_READ_INLINE)
    if not pending:
        return branches, []

    index.build_in_background()
    return branches, [BRANCHES_BUILDING.format(count=pending)]


def _branches_heading(places: list[Place], found_here: bool) -> str:
    """The line before the matches on other branches, such as "not in this checkout; found on 2 other branches:"."""
    count = len({place.origin.name for place in places if isinstance(place.origin, OnBranch)})
    branches = "1 other branch" if count == 1 else f"{count} other branches"
    return f"also on {branches}:" if found_here else f"not in this checkout; found on {branches}:"


def _fetch_note(index: Index, branches: list[git.Branch]) -> list[str]:
    """
    When remote-tracking branches were searched, a line saying when they were last fetched, so a missing match isn't
    read as missing code.
    """
    if not any(branch.remote for branch in branches):
        return []

    fetched = git.last_fetch(index.root)
    if fetched is None:
        return ["remotes not fetched since the clone"]
    return [f"remotes fetched {age(int(fetched))}"]


def _found_lines(
    hits: list[SymbolHit],
    paths: list[Place],
    limit: int,
) -> list[str]:
    """The output of find: symbol hits grouped by file, then the files whose path matched."""
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

    return output


def _find_at_ref(
    index: Index,
    query: str,
    kind: str | None,
    path: str | None,
    limit: int,
    ref: str,
) -> str:
    """find, on the files at a git ref. Only the files that contain the name are parsed."""
    words = query.split()
    if not words:
        return "Give a symbol name or part of a file path."

    parent, name = _split_qualified("".join(words))
    words = words if len(words) > 1 else [name]
    commit, tree = _tree_at(index, ref)

    symbols = [
        hit
        for hit in _symbols_at(index, commit, tree, ref, max(words, key=len), path)
        if _parent_and_kind_match(hit, parent, kind)
    ]
    hits = _ranked(
        [
            [hit for hit in symbols if hit.name.lower() == name.lower()],
            [hit for hit in symbols if hit.name.lower().startswith(name.lower())],
            [hit for hit in symbols if all(word.lower() in hit.name.lower() for word in words)],
        ]
    )
    paths = [] if kind else _paths_at(tree, ref, words, path)

    if not hits and not paths:
        return f"Nothing named or containing {query!r} at {ref}."
    return "\n".join(_found_lines(hits, paths, limit))


def grep(
    index: Index,
    pattern: str,
    regex: bool = False,
    path: str | None = None,
    limit: int = 40,
    deleted: bool = False,
    commits: bool = False,
    branches: bool = False,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> str:
    """
    Lines containing the pattern, each under the symbol it sits in. Deleted
    files are searched when asked for, or when no live file matches. Other
    branches are searched when asked for, or when no live or deleted file matches.
    The installed dependencies are searched only when deps asks for them.
    """
    index.refresh()

    try:
        found = _text_search(index, pattern, regex, path, limit, deleted)
    except re.error as error:
        return f"Invalid regex: {error}"

    notes: list[str] = []
    if deps != DependencyReach.FALLBACK:
        installed = dependencies.installed(index, deps == DependencyReach.ALL)
        packages = dependencies.package_rows(index, installed.packages)
        places = [place for place in dependencies.source_places(index, packages) if path_matches(place.path, path)]
        with index.connect() as session:
            found.add(
                _matching_lines(session, _file_texts(index, places), _matcher(pattern, regex)[0], limit, len(places))
            )
        notes = list(installed.notes)

    output = [found.summary(), *found.lines] if found.lines else []
    if branches or not found.lines:
        on_branches, branch_notes = _grep_on_branches(index, pattern, regex, path, limit, bool(found.lines))
        output.extend(on_branches)
        notes.extend(branch_notes)
    if commits:
        output.extend(_commits_changing(index, pattern))
    if not output:
        output.append(f"No text matches for {pattern!r} in live or deleted files or on other branches.")

    # Only a search of deleted files can miss what the unfinished history build hasn't read yet.
    note = _history_note(index) if deleted else ""
    return "\n".join([*output, *notes]) + note


def _grep_on_branches(
    index: Index,
    pattern: str,
    regex: bool,
    path: str | None,
    limit: int,
    found_here: bool,
) -> tuple[list[str], list[str]]:
    """
    The output of grep on other branches, empty when no branch has a match that the working tree lacks, and a note
    when some branches aren't indexed yet.
    """
    branches, notes = _read_branches(index)
    if not branches:
        return [], notes

    matcher, literals = _matcher(pattern, regex)
    with index.connect() as session:
        places = _small_enough(index, [place for place in session.branch_places() if path_matches(place.path, path)])
        found = _matching_lines(session, _branch_texts(index, places, literals), matcher, limit, len(places))

    if not found.lines:
        return [], notes
    heading = _branches_heading(found.places, found_here)
    return [heading, found.summary(), *found.lines, *_fetch_note(index, branches)], notes


@dataclass(slots=True)
class TextMatches:
    """
    The output lines of a text search, and how many lines and files matched in all. A search shows up to its limit
    but counts further, so the reader knows how much was left out.
    """

    lines: list[str]
    # The files that matched, shown or not.
    places: list[Place] = field(default_factory=list[Place])
    shown: int = 0
    hits: int = 0
    files: int = 0
    # Candidate files that weren't read, because counting stopped at settings.COUNT_FILES.
    unread: int = 0

    def add(self, other: "TextMatches") -> None:
        """Add another search's lines and counts to this one's."""
        self.lines.extend(other.lines)
        self.places.extend(other.places)
        self.shown += other.shown
        self.hits += other.hits
        self.files += other.files
        self.unread += other.unread

    def summary(self) -> str:
        """The first line of the output, such as "112 hits in 31 files, 40 shown. Use --limit or --path to see more"."""
        counted = f"{_plural(self.hits, 'hit')} in {_plural(self.files, 'file')}"
        if self.unread:
            counted += f" and {_plural(self.unread, 'more file')} not read"
        if self.shown == self.hits and not self.unread:
            return counted

        return f"{counted}, {self.shown} shown. Use --limit or --path to see more."


def _text_search(
    index: Index,
    pattern: str,
    regex: bool,
    path: str | None,
    limit: int,
    deleted: bool,
) -> TextMatches:
    """The lines a text search matched, with no lines when nothing matches."""
    matcher, literals = _matcher(pattern, regex)

    # Installed dependencies would bury the repository's own code. grep reads them only when asked to.
    in_dependencies = _in_dependencies(index)

    with index.connect() as session:
        live = [
            place
            for place in session.live_places_with_text(literals)
            if path_matches(place.path, path) and not in_dependencies(place.path)
        ]
        texts = ((place, index.read_live(place.path)) for place in live)

        found = _matching_lines(session, texts, matcher, limit, len(live))
        if found.lines and not deleted:
            return found

        if not literals:
            if found.lines:
                found.lines.append("Deleted files need a pattern with 3 or more plain characters in a row.")
            return found

        gone = [place for place in session.deleted_places_with_text(literals) if path_matches(place.path, path)]
        found.add(_matching_lines(session, _git_texts(index, gone), matcher, limit, len(gone)))

    return found


def _in_dependencies(index: Index) -> Callable[[str], bool]:
    """
    A test for whether a path in the working tree is inside a folder of installed dependencies, such as vendor/.
    Searches leave those out, even when they are committed, so dependency code is never shown as the repository's.
    """
    folders = tuple(dependencies.dependency_folders(index))
    return lambda path: path.startswith(folders) or "/node_modules/" in path


def _matcher(pattern: str, regex: bool) -> tuple[re.Pattern[str], list[str]]:
    """The compiled pattern, and the plain text every match must contain, for the trigram index to look up."""
    # The search ignores case unless the pattern has a capital letter in it. An escape like \S is not a capital.
    letters = re.sub(r"\\.", "", pattern) if regex else pattern
    flags = 0 if any(letter.isupper() for letter in letters) else re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    literals = required_literals(pattern) if regex else [pattern] if len(pattern) >= 3 else []
    return matcher, literals


def _file_texts(index: Index, places: list[Place]) -> Iterator[tuple[Place, str | None]]:
    """The text of files in installed dependencies, read from disk in order."""
    return ((place, _read(index, place)) for place in places)


def _branch_texts(
    index: Index,
    places: list[Place],
    literals: list[str],
) -> Iterator[tuple[Place, str | None]]:
    """
    The text of files on other branches, read from git in order. Their text isn't indexed, so every file is read.
    A file without the pattern's plain text is passed over before it is decoded, which is most of the cost.
    """
    required = [literal.lower().encode() for literal in literals]

    for place, (_, content) in zip(places, git.read_blobs(index.root, [place.oid for place in places])):
        if content is None or not all(literal in content.lower() for literal in required):
            yield place, None
        else:
            yield place, content.decode("utf-8", "replace")


def _small_enough(index: Index, places: list[Place]) -> list[Place]:
    """The places whose content is no bigger than settings.MAX_BYTES, which a text search reads."""
    sizes = git.blob_sizes(index.root, [place.oid for place in places])
    return [place for place in places if sizes.get(place.oid, settings.MAX_BYTES + 1) <= settings.MAX_BYTES]


def _git_texts(index: Index, places: list[Place]) -> Iterator[tuple[Place, str | None]]:
    """The text of files that git holds, read in order. Only the first settings.GIT_FILES_READ are read."""
    blobs = git.read_blobs(index.root, [place.oid for place in places[: settings.GIT_FILES_READ]])
    return (
        (place, content.decode("utf-8", "replace") if content else None) for place, (_, content) in zip(places, blobs)
    )


def callers(
    index: Index,
    target: str,
    path: str | None = None,
    limit: int = 40,
) -> str:
    """
    The lines that call or reference a symbol, each under the symbol it sits in. target is a name or
    "Class.method". The definitions, and mentions in comments, are left out. A line that may not call this symbol,
    such as a method called on a variable whose class isn't known, ends with POSSIBLE_MARK.
    """
    index.refresh()
    parent, name = _split_qualified(target)

    with index.connect() as session:
        named = [hit for hit in session.symbols_named(name, None, None, None) if hit.place.is_live]
        targets = [hit for hit in named if not parent or (hit.parent or "").lower() == parent.lower()]
        if not targets:
            return f"No symbol called {target!r}. Use find to search by part of a name."

        # A method name given without its class stands for that class when only one class defines it.
        owners = {hit.parent for hit in targets}
        owner = parent or (owners.pop() if len(owners) == 1 else None)
        is_member = any(hit.parent and hit.kind not in TYPE_KINDS for hit in targets)
        definitions = {(hit.place.path, hit.line) for hit in named}

        matcher = re.compile(rf"(?<![\w$]){re.escape(name)}(?!\w)")
        literals = [name] if len(name) >= 3 else []
        in_dependencies = _in_dependencies(index)
        live = [
            place
            for place in session.live_places_with_text(literals)
            if path_matches(place.path, path) and not in_dependencies(place.path)
        ]

        found = TextMatches([])
        for read, place in enumerate(live):
            # Once the limit is shown, the files after it are read only to count their calls, up to a point.
            if found.shown >= limit and read >= settings.COUNT_FILES:
                found.unread = len(live) - read
                break

            text = index.read_live(place.path)
            if text is None:
                continue

            symbols = session.symbols_in_blob(place.blob_row)
            calls = _calls_in(place.path, text, matcher, definitions, owner, is_member, symbols)
            if not calls:
                continue

            found.hits += len(calls)
            found.files += 1
            if found.shown < limit:
                shown = calls[: limit - found.shown]
                marks = {number: POSSIBLE_MARK for number, _, certain in shown if not certain}
                found.lines.extend(_lines_under_symbols(place.ref, symbols, [(n, line) for n, line, _ in shown], marks))
                found.shown += len(shown)

    if not found.lines:
        return f"Nothing calls or references {target!r} in live files."
    return "\n".join([found.summary(), *found.lines])


def _calls_in(
    path: str,
    text: str,
    matcher: re.Pattern[str],
    definitions: set[tuple[str, int]],
    owner: str | None,
    is_member: bool,
    symbols: list[SymbolHit],
) -> list[tuple[int, str, bool]]:
    """
    The lines of one file that call or reference the symbol, as (line number, line, certain). A line with several
    matches takes the most certain one.
    """
    lines = split_lines(text)
    matches = [
        (number, match.start(), match.end())
        for number, line in enumerate(lines, 1)
        if (path, number) not in definitions
        for match in matcher.finditer(line)
    ]
    roles = text_roles(path, text, [(number, column + 1) for number, column, _ in matches])

    best: dict[int, bool] = {}
    for (number, column, end), role in zip(matches, roles):
        if role == "comment":
            continue
        if role == "string" and not _names_a_callable(lines[number - 1], column, end):
            continue

        enclosing = _innermost([symbol for symbol in symbols if symbol.kind in TYPE_KINDS], number)
        certainty = _call_certainty(lines[number - 1][:column], owner, is_member, enclosing.name if enclosing else None)
        if certainty is not None:
            best[number] = best.get(number, False) or (certainty and role == "code")

    return [(number, lines[number - 1], certain) for number, certain in sorted(best.items())]


# A string that names a callable ends at the name, and has nothing before the name but a class and "::", ".",
# "@" or "->", as in 'Deepgram::transcribe', 'transcribe' or [$this, 'transcribe'].
CALLABLE_STRING = re.compile(r"""["'][\w\\]*(::|\.|@|->)?$""")


def _names_a_callable(line: str, start: int, end: int) -> bool:
    """
    True when the match from start to end, inside a string, is a callable's name, such as 'Deepgram::transcribe',
    and not a word in text, such as 'ai-scribe-transcribe-chunk'.
    """
    return bool(CALLABLE_STRING.search(line[:start])) and line[end : end + 1] in ("'", '"')


def _call_certainty(
    before: str,
    owner: str | None,
    is_member: bool,
    enclosing: str | None,
) -> bool | None:
    """
    Whether a match is certainly the symbol (True), may be it (False) or is not it (None). before is the text before
    the match on its line, owner the class that defines the symbol and enclosing the class the match sits in.
    """
    receiver = RECEIVER.search(before)
    if receiver is None:
        # A method is called through sonething that holds it. A bare name of a method is likely a function.
        return not is_member

    holder, operator = receiver.groups()
    holder = holder.rsplit("\\", 1)[-1].lower()
    if operator == "\\":
        # A namespace, as in App\Deepgram.
        return True
    if not is_member:
        # A function called through something, such as module.function(), may be another symbol with that name.
        return False

    if holder in {"$this", "this", "self", "static"}:
        return owner is not None and enclosing is not None and enclosing.lower() == owner.lower()
    if owner is not None and holder == owner.lower():
        return True
    if holder == "parent":
        return False
    if operator == "::" or (operator == "." and receiver.group(1)[:1].isupper()):
        # A static call on another class.
        return None

    return False


def outline(
    index: Index,
    path: str,
    ref: str | None = None,
) -> str:
    """Every symbol in one file with its line range, indented by nesting. With a ref, the file is read at that ref."""
    if ref is not None:
        return _outline_at_ref(index, path, ref)

    index.refresh()
    return _outline(index, path)


def _outline(index: Index, path: str) -> str:
    with index.connect() as session:
        places = _resolve_path(session, path)
        if len(places) != 1:
            return _path_choices(path, places)
        symbols = session.symbols_in_blob(places[0].blob_row)

    return _outline_lines(places[0], symbols)


def _outline_at_ref(
    index: Index,
    path: str,
    ref: str,
) -> str:
    places = _resolve_tree_path(_tree_at(index, ref)[1], ref, path)
    if len(places) != 1:
        return _path_choices(path, places)

    text = _read(index, places[0])
    symbols = extract(places[0].path, text) if text is not None else []
    return _outline_lines(places[0], [_hit(places[0], symbol) for symbol in symbols])


def _outline_lines(place: Place, symbols: list[SymbolHit]) -> str:
    if not symbols:
        return f"{place.ref}\n  (no symbols found; use show to read it)"

    output = [place.ref]
    open_ends: list[int] = []
    for hit in symbols:
        while open_ends and hit.line > open_ends[-1]:
            open_ends.pop()
        output.append(f"{'  ' * (len(open_ends) + 1)}{hit.line}-{hit.end_line} {hit.signature}")
        open_ends.append(hit.end_line)

    return "\n".join(output)


def show(
    index: Index,
    target: str,
    path: str | None = None,
    ref: str | None = None,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> str:
    """
    The source of one symbol, a line range or a whole file. target is a symbol
    name ("bandana", "BananaBender.bandana"), "path:45-80" or a path. Deleted
    files are read from git. With a ref, the source is read at that ref. deps
    says when a symbol is looked for in the installed dependencies.
    """
    if ref is not None:
        return _show_at_ref(index, target, path, ref)

    index.refresh()
    return _show(index, target, path, deps)


def _show(
    index: Index,
    target: str,
    path: str | None,
    deps: DependencyReach,
) -> str:
    in_dependencies = _in_dependencies(index)
    with index.connect() as session:
        line_range = LINE_RANGE.fullmatch(target)
        places = _resolve_path(session, line_range.group(1) if line_range else target)

        if line_range and len(places) != 1:
            return _path_choices(line_range.group(1), places)

        if len(places) == 1:
            first = int(line_range.group(2)) if line_range else 1
            last = int(line_range.group(3) or first) if line_range else None
            return _source(index, places[0], first, last)

        parent, name = _split_qualified(target)
        hits = [
            hit
            for hit in _find_symbols(session, name, [name], parent, None, path)
            if hit.name.lower() == name.lower() and not in_dependencies(hit.place.path)
        ]

    notes: list[str] = []
    if deps != DependencyReach.FALLBACK or not hits:
        package_hits, notes = _dependency_symbols(index, name, [name], parent, None, deps)
        # When dependencies were asked for, their source is what the reader wants to see first.
        hits = (
            _ranked(package_hits[:1]) + hits if deps != DependencyReach.FALLBACK else hits + _ranked(package_hits[:1])
        )

    if not hits:
        if len(places) > 1:
            return _path_choices(target, places)
        return "\n".join([f"No symbol or file called {target!r}. Use find to search by part of a name.", *notes])

    return "\n".join([_show_hits(index, hits), *notes])


def _show_at_ref(
    index: Index,
    target: str,
    path: str | None,
    ref: str,
) -> str:
    commit, tree = _tree_at(index, ref)
    line_range = LINE_RANGE.fullmatch(target)
    places = _resolve_tree_path(tree, ref, line_range.group(1) if line_range else target)

    if line_range and len(places) != 1:
        return _path_choices(line_range.group(1), places)

    if len(places) == 1:
        first = int(line_range.group(2)) if line_range else 1
        last = int(line_range.group(3) or first) if line_range else None
        return _source(index, places[0], first, last)

    parent, name = _split_qualified(target)
    symbols = _symbols_at(index, commit, tree, ref, name, path)
    hits = _ranked(
        [[hit for hit in symbols if hit.name.lower() == name.lower() and _parent_and_kind_match(hit, parent, None)]]
    )

    if not hits:
        if len(places) > 1:
            return _path_choices(target, places)
        return f"No symbol or file called {target!r} at {ref}."
    return _show_hits(index, hits)


def _show_hits(index: Index, hits: list[SymbolHit]) -> str:
    """The source of the best hit, and where the others are."""
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
    """
    Split "Parent::name", "Parent.name" or "Parent#name" into its two parts. The index keeps a class's name without
    its namespace, so "Phinx\\Db\\Table.addIndex" gives "Table" and "addIndex".
    """
    parts = re.split(r"::|[.#]", name)
    if len(parts) >= 2 and all(parts[-2:]):
        return parts[-2].rsplit("\\", 1)[-1], parts[-1]
    return None, name.rsplit("\\", 1)[-1]


def _find_symbols(
    session: IndexSession,
    name: str,
    words: list[str],
    parent: str | None,
    kind: str | None,
    path: str | None,
    scope: Scope = Scope.WORKING_TREE,
) -> list[SymbolHit]:
    """Symbols matching a name, best first: exact, then prefix, then containing every word."""
    return _ranked(
        [
            session.symbols_named(name, parent, kind, path, scope),
            session.symbols_starting_with(name, parent, kind, path, scope),
            session.symbols_containing_all(words, parent, kind, path, scope),
        ]
    )


def _find_abbreviated(
    session: IndexSession,
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

    symbols = session.symbols_containing_in_order(list(letters), parent, kind, path)
    hits = _ranked([[hit for hit in symbols if _is_abbreviation(letters, hit.name)]])

    if kind:
        return hits, []

    places = session.places_containing_in_order(list(letters))
    paths = [place for place in places if _is_abbreviation(letters, place.path) and path_matches(place.path, path)]
    return hits, sorted(paths, key=lambda place: (place.is_deleted, len(place.path)))


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
    ranked: dict[tuple[str, int, str, int], tuple[tuple[int, bool, int, bool, int], SymbolHit]] = {}
    for tier, hits in enumerate(tiers):
        for hit in hits:
            # Two branches can hold different versions of one path, so the content is part of the key.
            key = (hit.place.path, hit.place.blob_row, hit.name, hit.line)
            if key in ranked:
                continue
            kind_rank = 0 if hit.kind in TYPE_KINDS else 1 if hit.kind in CALLABLE_KINDS else 2
            rank = (tier, hit.place.is_deleted, kind_rank, _is_secondary(hit.place.path), len(hit.place.path))
            ranked[key] = (rank, hit)

    return [hit for _, hit in sorted(ranked.values(), key=lambda entry: entry[0])]


def _find_paths(
    session: IndexSession,
    words: list[str],
    path_filter: str | None,
    scope: Scope = Scope.WORKING_TREE,
) -> list[Place]:
    """Live then deleted files whose path contains every word, shortest path first."""
    return sorted(
        (place for place in session.places_containing_all(words, scope) if path_matches(place.path, path_filter)),
        key=lambda place: (place.is_deleted, len(place.path)),
    )


def _resolve_path(session: IndexSession, path: str) -> list[Place]:
    """The file a path names: an exact match if there is one, or else every file whose path ends with it."""
    deleted = DELETED_REF.fullmatch(path)
    if deleted:
        commit, path = deleted.groups()
        return [
            place
            for place in _resolve_path(session, path)
            if isinstance(place.origin, Deleted) and place.origin.commit.startswith(commit)
        ]

    path = path.removeprefix("./")
    exact = session.places_at_path(path)
    if exact:
        return exact[:1]
    return session.places_ending_with(path)


def _path_choices(path: str, places: list[Place]) -> str:
    if not places:
        return f"No file matches {path!r}. Use find to search for it."
    return f"{path!r} matches several files:\n" + "\n".join(f"  {place.ref}" for place in places[:20])


def _matching_lines(
    session: IndexSession,
    texts: Iterator[tuple[Place, str | None]],
    matcher: re.Pattern[str],
    limit: int,
    candidates: int,
) -> TextMatches:
    """
    The output lines for the candidate files that really match, until limit matching lines are shown. The files after
    that are only counted. candidates is how many files texts holds.
    """
    found = TextMatches([])
    read = 0

    for place, text in texts:
        if found.shown >= limit and read >= settings.COUNT_FILES:
            break
        read += 1
        if text is None:
            continue

        matches = [(number, line) for number, line in enumerate(split_lines(text), 1) if matcher.search(line)]
        if not matches:
            continue

        found.hits += len(matches)
        found.files += 1
        found.places.append(place)
        if found.shown >= limit:
            continue

        symbols = session.symbols_in_blob(place.blob_row)
        definitions = {symbol.line: DEFINITION_MARK for symbol in symbols if _is_defined_by(symbol, matches, matcher)}
        found.lines.extend(_lines_under_symbols(place.ref, symbols, matches[: settings.LINES_PER_FILE], definitions))
        found.shown += min(len(matches), settings.LINES_PER_FILE)
        if len(matches) > settings.LINES_PER_FILE:
            found.lines.append(f"({len(matches) - settings.LINES_PER_FILE} more matching lines in {place.ref})")

    found.unread = candidates - read
    return found


def _is_defined_by(
    symbol: SymbolHit,
    matches: list[tuple[int, str]],
    matcher: re.Pattern[str],
) -> bool:
    """
    True when a matching line is where the symbol is defined, and the match is about the symbol: the pattern matches
    its name, as "bandana" doies, or the matched text holds its name, as "function bandana" does.
    """
    line = next((line for number, line in matches if number == symbol.line), None)
    if line is None:
        return False

    matched = matcher.search(line)
    return bool(matcher.search(symbol.name)) or (matched is not None and symbol.name in matched.group(0))


def _lines_under_symbols(
    ref: str,
    symbols: list[SymbolHit],
    matches: list[tuple[int, str]],
    marks: dict[int, str] | None = None,
) -> list[str]:
    """
    One file's matching lines as "path:line:text". Where the enclosing symbol changes, its first line comes before
    them as a context line, "path-line-text", the way grep and ripgrep print context. marks holds text to add to
    the end of some lines, by line number.
    """
    output: list[str] = []
    last_symbol: SymbolHit | None = None

    for number, line in matches:
        symbol = _innermost(symbols, number)
        if symbol is not None and symbol != last_symbol and symbol.line != number:
            output.append(f"{ref}-{symbol.line}-{symbol.signature}")
        last_symbol = symbol

        text = line.strip()[: settings.MAX_LINE_LENGTH] + (marks or {}).get(number, "")
        output.append(f"{ref}:{number}:{text}")

    return output


def _innermost(symbols: list[SymbolHit], line: int) -> SymbolHit | None:
    around = [symbol for symbol in symbols if symbol.line <= line <= symbol.end_line]
    return min(around, key=lambda symbol: symbol.end_line - symbol.line) if around else None


def _source(index: Index, place: Place, first: int, last: int | None) -> str:
    """Lines first to last of a file, under a "path:first-last" heading. last=None means to the end."""
    text = _read(index, place)
    if text is None:
        return f"{place.ref} can't be shown: it is binary, larger than 1MB, or no longer in git."

    lines = split_lines(text)
    last = min(last or len(lines), len(lines))
    shown_last = min(last, first + settings.SHOW_MAX_LINES - 1)
    output = [f"{place.ref}:{first}-{shown_last}", *lines[first - 1 : shown_last]]

    if shown_last < last:
        output.append(f"({last - shown_last} more lines. Continue with show {place.ref}:{shown_last + 1}-{last})")

    return "\n".join(output)


def _read(index: Index, place: Place) -> str | None:
    """The text of a file, from disk or from git. None when it is binary, too big or gone."""
    match place.origin:
        case Live():
            return index.read_live(place.path)
        case InPackage(folder=folder):
            return index.read_text(folder / place.path)
        case Deleted() | AtRef() | OnBranch():
            content = next(iter(git.read_blobs(index.root, [place.oid])))[1]
            return content.decode("utf-8", "replace") if content else None
        case _:
            assert_never(place.origin)


def _tree_at(index: Index, ref: str) -> tuple[str, dict[str, str]]:
    """The commit a ref names, and every file at it with its blob id. Raises git.UnknownRef when there is no commit."""
    if not index.in_git:
        raise git.UnknownRef(f"--ref needs a git repository, and {index.root} is not one")

    commit = git.commit_of(index.root, ref)
    return commit, git.files_at(index.root, commit)


def _resolve_tree_path(
    tree: dict[str, str],
    ref: str,
    path: str,
) -> list[Place]:
    """The file a path names at a rf, the way _resolve_path finds it in the working tree."""
    path = path.removeprefix(ref + ":").removeprefix("./")
    if path in tree:
        return [Place(path, tree[path], 0, AtRef(ref))]

    return [Place(known, oid, 0, AtRef(ref)) for known, oid in tree.items() if known.endswith("/" + path)]


def _paths_at(
    tree: dict[str, str],
    ref: str,
    words: list[str],
    path_filter: str | None,
) -> list[Place]:
    """Files at a ref whose path contains every word, shortest path first."""
    found = [
        Place(path, oid, 0, AtRef(ref))
        for path, oid in tree.items()
        if all(word.lower() in path.lower() for word in words) and path_matches(path, path_filter)
    ]
    return sorted(found, key=lambda place: len(place.path))


def _symbols_at(
    index: Index,
    commit: str,
    tree: dict[str, str],
    ref: str,
    text: str,
    path_filter: str | None,
) -> list[SymbolHit]:
    """
    Every symbol in the files at a ref that contain text. git grep picks those files, so a search
    parses only them and needs no index of the ref. Only the first settings.GIT_FILES_READ are parsed,
    shortest path first, so a common word can't make one search parse the whole tree.
    """
    places = sorted(
        (
            Place(path, tree[path], 0, AtRef(ref))
            for path in git.paths_containing(index.root, commit, text)
            if path in tree and path_matches(path, path_filter)
        ),
        key=lambda place: len(place.path),
    )[: settings.GIT_FILES_READ]
    hits: list[SymbolHit] = []

    for place, (_, content) in zip(places, git.read_blobs(index.root, [place.oid for place in places])):
        if content is not None and len(content) <= settings.MAX_BYTES:
            symbols = extract(place.path, content.decode("utf-8", "replace"))
            hits.extend(_hit(place, symbol) for symbol in symbols)

    return hits


def _hit(place: Place, symbol: Symbol) -> SymbolHit:
    return SymbolHit(place, symbol.name, symbol.kind, symbol.parent, symbol.line, symbol.end_line, symbol.signature)


def _parent_and_kind_match(hit: SymbolHit, parent: str | None, kind: str | None) -> bool:
    """The filters the index applies in SQL, for symbols that were parsed without the index."""
    if parent and (hit.parent or "").lower() != parent.lower():
        return False
    return not kind or hit.kind == kind.lower()


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


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _is_secondary(path: str) -> bool:
    """Tests and third-party code rank below the code a search is most likely after."""
    lowered = path.lower()
    return any(part in lowered for part in ("test", "spec", "vendor/", "node_modules/", "fixture"))
