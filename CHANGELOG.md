# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
