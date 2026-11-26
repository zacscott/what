"""Searches the text of files: the working tree, deleted files, other branches and dependencies."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final, NamedTuple

from what import dependencies, git
from what.config import settings
from what.database import IndexSession
from what.dependencies import DependencyReach
from what.index import Index
from what.models import Place, SymbolHit, path_matches
from what.output import Line, LineKind, Mark, Result, note, nothing, plural
from what.search.branches import Section, branches_heading, fetch_note, read_branches
from what.search.lines import lines_under_symbols
from what.search.paths import dependency_paths, read_place
from what.search.patterns import Case, Matcher, matcher_for
from what.symbols import split_lines

HISTORY_BUILDING: Final = "(The index of deleted files is not up to date, so deleted files may be missing.)"


class Listing(NamedTuple):
    """
    How a text search lists what it matched. limit is the most matches shown. files_only lists each matching file
    once, without its lines. context is how many lines before and after each match are shown with it.
    """

    limit: int
    files_only: bool = False
    context: int = 0

    @property
    def has_breaks(self) -> bool:
        """True when a break comes between two files, which is when lines of context are shown."""
        return self.context > 0 and not self.files_only


def grep(
    index: Index,
    pattern: str,
    *,
    regex: bool = False,
    case: Case = Case.SMART,
    word: bool = False,
    path: str | None = None,
    limit: int = 40,
    files_only: bool = False,
    context: int = 0,
    deleted: bool = False,
    commits: bool = False,
    branches: bool = False,
    deps: DependencyReach = DependencyReach.FALLBACK,
) -> Result:
    """
    Lines containing the pattern, each under the symbol it sits in. Deleted
    files are searched when asked for, or when no live file matches. Other
    branches are searched when asked for, or when no live or deleted file matches.
    The installed dependencies are searched only when deps asks for them.

    word matches the pattern only as a whole word. files_only lists each matching file once, and context is how
    many lines around each match are shown. Raises InvalidPattern for a regex that doesn't compile.
    """
    matcher = matcher_for(pattern, regex=regex, case=case, word=word)
    listing = Listing(limit=limit, files_only=files_only, context=context)

    index.refresh()
    found = text_search(index, matcher, path=path, deleted=deleted, listing=listing)

    notes: list[str] = []
    if deps != DependencyReach.FALLBACK:
        installed = dependencies.installed(index, transitive=deps == DependencyReach.ALL)
        packages = dependencies.package_rows(index, installed.packages)
        places = [place for place in dependencies.source_places(index, packages) if path_matches(place.path, path)]
        with index.connect() as session:
            found.add(
                _matching_lines(
                    session,
                    _file_texts(index, places),
                    matcher=matcher,
                    listing=listing,
                    candidates=len(places),
                ),
                with_break=listing.has_breaks,
            )
        notes = list(installed.notes)

    output = [note(found.summary(files_only=files_only)), *found.lines] if found.lines else []
    if branches or not found.lines:
        on_branches, branch_notes = _grep_on_branches(
            index, matcher, path=path, listing=listing, found_here=bool(found.lines)
        )
        output.extend(on_branches)
        notes.extend(branch_notes)

    matched = bool(output)
    if commits:
        changing = _commits_changing(index, pattern)
        output.extend(changing.lines)
        matched = matched or changing.found
    if not matched:
        output.insert(0, note(f"No text matches for {pattern!r} in live or deleted files or on other branches."))

    output.extend(map(note, notes))
    # Only a search of deleted files can miss what the unfinished history build hasn't read yet.
    if deleted and not index.history_is_current():
        output.append(note(HISTORY_BUILDING))

    return Result(output, found=matched)


def _grep_on_branches(
    index: Index,
    matcher: "Matcher",
    *,
    path: str | None,
    listing: Listing,
    found_here: bool,
) -> Section:
    """
    The output of grep on other branches, empty when no branch has a match that the working tree lacks, and a note
    when some branches aren't indexed yet.
    """
    branches, notes = read_branches(index)
    if not branches:
        return Section([], notes)

    literals = matcher.literals
    with index.connect() as session:
        places = [place for place in session.branch_places_with_text(literals) if path_matches(place.path, path)]
        if not literals:
            # The text index holds only files of settings.MAX_BYTES or less. Without it, git says how big each is.
            places = _small_enough(index, places)
        found = _matching_lines(
            session,
            _branch_texts(index, places, literals=literals),
            matcher=matcher,
            listing=listing,
            candidates=len(places),
        )

    if not found.lines:
        return Section([], notes)

    return Section(
        [
            note(branches_heading(found.places, found_here=found_here)),
            note(found.summary(files_only=listing.files_only)),
            *found.lines,
            *map(note, fetch_note(index, branches)),
        ],
        notes,
    )


@dataclass(slots=True)
class TextMatches:
    """
    The output lines of a text search, and how many lines and files matched in all. A search shows up to its limit
    but counts further, so the reader knows how much was left out.
    """

    lines: list[Line]
    # The files that matched, shown or not.
    places: list[Place] = field(default_factory=list[Place])
    shown: int = 0
    hits: int = 0
    files: int = 0
    # Candidate files that weren't read, because counting stopped at settings.COUNT_FILES.
    unread: int = 0

    def add(self, other: "TextMatches", *, with_break: bool = False) -> None:
        """
        Add another search's lines and counts to this one's. with_break puts a break between the two sets of
        lines, as a search with context lines has between any two files.
        """
        if with_break and self.lines and other.lines:
            self.lines.append(Line(LineKind.BREAK))
        self.lines.extend(other.lines)
        self.places.extend(other.places)
        self.shown += other.shown
        self.hits += other.hits
        self.files += other.files
        self.unread += other.unread

    def summary(self, *, files_only: bool = False) -> str:
        """
        The first line of the output, such as "112 hits in 31 files, 40 shown. Use --limit or --path to see more".
        With files_only, shown counts files and not hits.
        """
        counted = f"{plural(self.hits, 'hit')} in {plural(self.files, 'file')}"
        if self.unread:
            counted += f" and {plural(self.unread, 'more file')} not read"
        if self.shown == (self.files if files_only else self.hits) and not self.unread:
            return counted

        shown = f"{plural(self.shown, 'file')} shown" if files_only else f"{self.shown:,} shown"
        return f"{counted}, {shown}. Use --limit or --path to see more."


def text_search(
    index: Index,
    matcher: "Matcher",
    *,
    path: str | None,
    deleted: bool,
    listing: Listing,
) -> TextMatches:
    """The lines a text search matched, with no lines when nothing matches."""
    literals = matcher.literals

    # Installed dependencies would bury the repository's own code. grep reads them only when asked to.
    in_dependencies = dependency_paths(index)

    with index.connect() as session:
        live = [
            place
            for place in session.live_places_with_text(literals)
            if path_matches(place.path, path) and not in_dependencies(place.path)
        ]
        texts = ((place, index.read_live(place.path)) for place in live)

        found = _matching_lines(session, texts, matcher=matcher, listing=listing, candidates=len(live))
        if found.lines and not deleted:
            return found

        if not literals:
            if found.lines:
                found.lines.append(note("Deleted files need a pattern with 3 or more plain characters in a row."))
            return found

        gone = [place for place in session.deleted_places_with_text(literals) if path_matches(place.path, path)]
        found.add(
            _matching_lines(session, _git_texts(index, gone), matcher=matcher, listing=listing, candidates=len(gone)),
            with_break=listing.has_breaks,
        )

    return found


def _file_texts(index: Index, places: list[Place]) -> Iterator[tuple[Place, str | None]]:
    """The text of files in installed dependencies, read from disk in order."""
    return ((place, read_place(index, place)) for place in places)


def _branch_texts(
    index: Index,
    places: list[Place],
    *,
    literals: list[str],
) -> Iterator[tuple[Place, str | None]]:
    """
    The text of files on other branches, read from git in order. A file without the pattern's plain text is passed
    over before it is decoded, which is most of the cost.
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


def _matching_lines(
    session: IndexSession,
    texts: Iterator[tuple[Place, str | None]],
    *,
    matcher: Matcher,
    listing: Listing,
    candidates: int,
) -> TextMatches:
    """
    The output lines for the candidate files that really match, until the listing's limit of matches is shown. The
    matches after that are only counted. candidates is how many files texts holds.
    """
    found = TextMatches([])
    read = 0

    for place, text in texts:
        if found.shown >= listing.limit and read >= settings.COUNT_FILES:
            break
        read += 1
        # The text index picks a file that holds a literal's trigrams anywhere, so about half the candidates lack
        # the literal itself. One look through the whole text is much cheaper than a search of every line.
        if text is None or not all(_holds(text, literal) for literal in matcher.literals):
            continue

        lines = split_lines(text)
        matches = [(number, line) for number, line in enumerate(lines, 1) if matcher.pattern.search(line)]
        if not matches:
            continue

        found.hits += len(matches)
        found.files += 1
        found.places.append(place)

        room = listing.limit - found.shown
        if room <= 0:
            continue
        if listing.files_only:
            found.lines.append(Line(LineKind.FILE, place=place))
            found.shown += 1
            continue

        shown = matches[: min(room, settings.LINES_PER_FILE)]
        if listing.has_breaks and found.lines:
            # grep puts the same break between the lines of two files.
            found.lines.append(Line(LineKind.BREAK))
        symbols = session.symbols_in_blob(place.blob_row)
        definitions = {
            symbol.line: Mark.DEFINITION
            for symbol in symbols
            if _is_defined_by(symbol, matches=matches, matcher=matcher.pattern)
        }
        found.lines.extend(
            lines_under_symbols(
                place,
                symbols,
                matches=shown,
                pattern=matcher.pattern,
                marks=definitions,
                source=lines,
                context=listing.context,
                left_out=[number for number, _ in matches[len(shown) :]],
            )
        )
        found.shown += len(shown)
        if len(matches) > len(shown):
            more = plural(len(matches) - len(shown), "more matching line")
            found.lines.append(note(f"({more} in {place.ref})"))

    found.unread = candidates - read
    return found


def _holds(text: str, literal: str) -> bool:
    """True when text contains the literal, ignoring case as a pattern does."""
    # Lowering both sides is several times faster than a pattern, and gives the same answer for ASCII.
    if text.isascii() and literal.isascii():
        return literal.lower() in text.lower()

    return re.search(re.escape(literal), text, re.IGNORECASE) is not None


def _is_defined_by(
    symbol: SymbolHit,
    *,
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


def _commits_changing(index: Index, text: str) -> Result:
    """The commits that added or removed the text, as notes. found is False when there is none to list."""
    if not index.in_git:
        return nothing("Commit search needs a git repository.")

    commits = git.commits_changing_text(index.root, text, limit=10, timeout=settings.COMMIT_SEARCH_SECONDS)

    if commits is None:
        return nothing(f"The commit search ran past {settings.COMMIT_SEARCH_SECONDS} seconds and was stopped.")
    if not commits:
        return nothing(f"No commit added or removed {text!r}.")

    return Result([note(f"Commits that added or removed {text!r}:"), *(note(f"  {commit}") for commit in commits)])
