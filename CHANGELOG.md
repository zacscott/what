# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-27

### Fixed

- A path that named no file, such as `what src/nope.py`, printed up to ten unrelated files, because only the `py` after the last dot was matched. A query with a `/` or a `.` in it is no longer matched against paths as an abbreviation. When `find` does list names a plain word may be short for, a first line says so.

- `what show src/a.py:0-3` printed a heading with no lines, and a range that ends before it starts printed nothing useful. A range now starts at line 1 at the lowest, and a range that ends before it starts is refused.

- `what grep text | head` could still print `BrokenPipeError` when Python flushed its output as it exited.

- `what <file> --ref <ref>` printed an empty outline for a file that defines nothing. It now shows the file, as it does in the working tree.

- `show` on an empty file printed the range `1-0`. It now says the file is empty.

- With `WHAT_ROOT` set, the background build indexed that folder even when the search ran in another one.

- A match far along a very long line was cut off, so the line shown did not hold it. The line now starts shortly before the match, after `...`.

- `grep --limit 3` printed up to 8 lines, because the limit was only checked between files. No more matches than the limit are shown now. In `find`, the files whose path matched count towards the limit too.

- A mistake in an option of `what <name>` printed "Try 'what lookup --help'", which names a command nobody types. It now prints the usage of `what` itself.

- A signature written over several lines was joined with stray spaces, as in `( index: Index, )`. It is now joined as it would be written on one line.

- A background build that failed was started again by every search, and its error went nowhere. The error is now in the log file, and a search waits five minutes before it starts the build again.

- After a checkout of another branch, the background index of deleted files read every deleted file from git again when a search ran during the build. On a 33,000 commit repository that took about 50 seconds after each checkout, and now takes about 4.

### Added

- `what status` shows what the index of the repository holds, whether its deleted files are up to date, whether a background build is running, and where the index and the log are.

- `what -C <folder>` runs in another folder, as `git -C` does. `what --version` prints the version.

- `grep` takes `-i`, `-s`, `-w`, `-l`, `-C <lines>` and `-e` or `-E` for `--regex`. With `-C`, every context line is the source line as it is, and a `--` line comes wherever two lines of output are not next to each other in a file, and between two files.

- `--json` on every search prints one JSON object for each line of output. An error such as an unknown ref, or a mistake in the options, is an object too, with the type `error`. It is printed on stderr as well.

- Every log record at the level ERROR or above is printed on stderr, so an error is never only in the log file. A command that fails with an exception exits with 2, and used to exit with 1, which means nothing was found.

- At a terminal, a search prints the matches of one file under its path, with the path, the line numbers and the matched text in colour. A pipe gets plain lines with no colour. `--color` picks one, and `NO_COLOR` turns colour off.

- At a terminal, a long read of the index shows how far it is on stderr, such as `what: reading files, 4,200 of 12,184`.

- A log file at `~/.local/state/what/what.log`, or under `$XDG_STATE_HOME`. It has a line for each command, with its duration and how it ended, and a line each time the index reads files again. A failed command is logged with its traceback. A grammar that can't be downloaded is logged too, since its files get no symbols.

- `find` and `grep` search other branches when the working tree has no match, and `--branches` searches them every time. They search the default branch and every local and remote-tracking branch that isn't merged into it and has a commit in the last year. A hit is shown only where the branch's version of the file differs from the working tree, and is labelled with the branch and the age of its last commit. When remote-tracking branches were searched, the output says when they were last fetched.

### Changed

- A search that found nothing exits with 1 and prints the reason on stderr, as `grep` does. It used to exit with 0. An error, such as an unknown ref or a regex that doesn't compile, exits with 2 and used to exit with 1.

- `what index` and `what prompt` are commands. The old spellings `what --index` and `what --prompt` still work. A symbol called `index`, `prompt` or `status` is searched for with `what find`.

- `--limit` must be 1 or more.

- `find` lists the names that are exactly the query first, then a `partial matches:` line, then the names that only start with the query or contain it.

- `grep` and `callers` print each line with its indentation.

- `show` prints each line as `line:text`, and `outline` prints each symbol as `first-last:signature`, both under the file's path. The other definitions that `show` lists are `path:line:signature`.

- The text index holds which files have each trigram and not where in the file. On a repository of 12,000 files and 840 branches the index went from 514MB to 160MB. `grep` now lists files in path order. Each index, and the index of dependencies, is built again on the first search after the upgrade.
- `callers` stops reading files once it has shown its limit, and its first line says how many files it did not read. On the same repository a search went from 4 seconds to 0.2.
- `grep` on other branches reads from git only the files that may match, because the text of branch files is now indexed. A `grep` with no match went from 1.3 seconds to 0.3.
- `find` and `show` with `--ref` take symbols from the index for every file at the ref whose content it holds, and parse only the rest. `find` at the remote's default branch went from 1.8 seconds to 0.4.
- A search no longer reads git's list of tracked files when git's index file has not changed since the last search. A name search went from about 170ms to about 120ms.

## [0.2.0] - 2026-11-25

### Fixed

- A symbol the grammar pack gives no named kind for, such as a C `#define` or `typedef`, was stored with a kind like `{"other":"constant"}`, so `--kind constant` missed it. It is now stored as its kind.

### Added

- `find` and `show` look in the installed dependencies when the repository has no match, and `--deps` searches them every time. Each hit is labelled with its package and version, such as `vendor: robmorgan/phinx@0.16.1`. `grep` searches the dependencies' text only with `--deps`. The installed versions come from `composer.lock`, `package-lock.json`, `uv.lock` and `poetry.lock`. Only direct dependencies are searched unless `--all-deps` is given. Their symbols are indexed once for each package version, in an index every repository shares. A lockfile with packages that aren't installed gets a one-line note.

- `what callers <symbol>` lists the lines that call or reference a symbol, each under the function it sits in. It leaves out the definitions and mentions in comments and docblocks. A call that may be to another symbol, such as a method called on an untyped variable, ends with `[possible]`.

- `find` and `grep` search other branches when the working tree has no match, and `--branches` searches them every time. They search the default branch and every local and remote-tracking branch that isn't merged into it and has a commit in the last year. Branch files have their symbols indexed but not their text. A hit is shown only where the branch's version of the file differs from the working tree, and is labelled with the branch and the age of its last commit. When remote-tracking branches were searched, the output says when they were last fetched.

- `--ref <ref>` on `find`, `show` and `outline` answers at a branch, tag or commit in place of the working tree. It reads the files from git and needs no index of the ref. An unknown ref exits with an error that names it.

- A qualified name with a namespace, such as `Phinx\Db\Table.addIndex`, matches the class without its namespace.

### Changed

- `--kind` accepts only the kinds the index holds, so a misspelt kind is an error and not an empty result.
- A big batch of files is parsed on every core, so the first index of a large repository is several times faster.
- `grep` no longer prints a traceback when its reader, such as `head`, stops reading.
- `grep` starts with the number of hits and files, such as `12 hits in 5 files, 4 shown`, so output cut by `head` or `--limit` says how much is missing.
- `grep` marks the line that defines a matching symbol with `[definition]`.
- The notice that the index of deleted files is not up to date appears only on `grep --deleted`.

## [0.1.1] - 2026-11-25

### Changed

- The searches now take an index session instead of a raw database connection, and the connection, schema setup and write transaction live behind one class.

## [0.1.0] - 2026-11-25

### Added

- A name search across symbols and files, with exact, prefix, substring and abbreviation matching.
- File outlines with line ranges, line-range output, and `show` for one symbol.
- Text search with regex support, grouped under the symbol that contains each match.
- An index of files deleted in git history, named by the commit that deleted them.
- An SQLite index that matches the working tree, including uncommitted edits.
- Directory mode for paths that are not git repositories.
- `what --prompt` for coding-harness instructions.
