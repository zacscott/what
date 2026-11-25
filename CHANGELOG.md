# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--ref <ref>` on `find`, `show` and `outline` answers at a branch, tag or commit in place of the working tree. It reads the files from git and needs no index of the ref. An unknown ref exits with an error that names it.

- A qualified name with a namespace, such as `Phinx\Db\Table.addIndex`, matches the class without its namespace.

### Changed

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
