# what

Fast context for your code harness.

`what` indexes a git repository by symbol and by text, and finds files that git history deleted. Your harness gets the definition or the lines it needs, instead of a whole file in its context.

## Install

```bash
uv tool install https://github.com/zacscott/what.git
```

OR

```bash
uvx --from https://github.com/zacscott/what.git what --help
```

## Add it to your harness

`what --prompt` prints the instructions your harness needs to use the command well. Add them to the file it reads at the start of a session:

```bash
# Claude Code
what --prompt >> CLAUDE.md

# other harnesses
what --prompt >> AGENTS.md
```

The block is about 800 characters. It tells the harness to use `what` before grep, glob or reading a whole file.

Include it in every repository by adding the block to your global file instead, such as `~/.claude/CLAUDE.md`.

## Use

```bash
what BananaBender
```

```
src/BananaBender.php:2:class BananaBender extends Lady
```

`what` takes a name, a file or a line range. A word that is also a command, like `index`, needs `what find index`.

```bash
what src/BananaBender.php:45-80  # those lines
what show Banana.bend            # one method
what grep "str_repeat("          # text, under the symbol it sits in
what show Banana.bend --ref origin/dev  # the method as it is on another branch
```

`--ref` takes anything git accepts: a branch, a tag, a commit, `origin/dev` or `HEAD~3`. It works on `find`, `show` and `outline`, and reads from git, so the ref needs no index of its own.

The first search builds the index. `what --index` builds it up front.

## How it works

- Symbols come from tree-sitter. Every language the grammar pack supports gets parsed.
- Text is in an SQLite FTS5 trigram index. The index picks the files that might match, then `what` reads only those files for the matching lines.
- Deleted files come from `git log --diff-filter=D`. Each one is indexed as it was just before the commit that removed it. They are named `COMMIT^:path`, so `git show` accepts the name.
- Before each search, `what` asks git which files changed and re-reads only those. There is no file watcher.
- Outside a git repository, `what` walks the directory and follows its `.gitignore` files. There are no deleted files.

The index lives in `~/.cache/what/` or `$XDG_CACHE_HOME`. It is a cache, so deleting it only costs a rebuild.

Linux and macOS only. Files over 1MB, binaries, minified files and lock files are indexed by path only.
