# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
