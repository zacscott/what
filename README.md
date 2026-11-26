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

`what prompt` prints the instructions your harness needs to use the command well. Add them to the file it reads at the start of a session:

```bash
# Claude Code
what prompt >> CLAUDE.md

# other harnesses
what prompt >> AGENTS.md
```

The block is about 1,900 characters. It tells the harness to use `what` before grep, glob or reading a whole file.

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
what callers Banana.bend         # the lines that call it
what grep "str_repeat("          # text, under the symbol it sits in
what show Banana.bend --ref origin/dev  # the method as it is on another branch
```

`--ref` takes anything git accepts: a branch, a tag, a commit, `origin/dev` or `HEAD~3`. It works on `find`, `show` and `outline`, and reads from git, so the ref needs no index of its own.

The first search builds the index. `what index` builds it up front, and at a terminal it shows how far it is. `what status` shows what the index holds, whether it is up to date and where its files are. `what -C <folder>` runs in another folder, as `git -C` does.

`grep` takes short options of grep and ripgrep: `-i`, `-s`, `-w`, `-l`, `-C 3`, and `-e` or `-E` for a regex.

## Output

A search across files prints one match per line, as `path:line:text`, with the line's own indentation. A line with `-` in place of `:` is context: the first line of the function a match sits in, or a line that `-C` asked for.

```
src/BananaBender.php-3-public function bandana(int $count): string
src/BananaBender.php:4:        return str_repeat('banana', $count);
```

A command about one file prints the path once, then `line:text`. `outline` prints `first-last:signature`.

```bash
what show BananaBender.bandana
```

```
src/BananaBender.php:3-5
3:    public function bandana(int $count): string {
4:        return str_repeat('banana', $count);
5:    }
```

`find` lists the names that are exactly the query first. The names that only start with it or contain it come after a `partial matches:` line.

At a terminal the matches of one file sit under its path, in colour. A pipe gets the plain lines above, so a harness always reads the same format. `--color always` and `--color never` pick one, and `NO_COLOR` turns colour off.

`--json` prints one object for each line, such as `{"type": "match", "path": "src/BananaBender.php", "line": 4, "text": "..."}`. A file that is not in the working tree also says where it comes from, in `deleted_in`, `branch`, `ref` or `package`.

A search exits with 0 when it found something, 1 when it found nothing and 2 for an error, as `grep` does. When it found nothing, it says so on stderr and prints nothing on stdout. An error always goes to stderr, and so does a mistake in the options. With `--json`, it is an object on stdout as well, such as `{"type": "error", "text": "..."}`.

## How it works

- Symbols come from tree-sitter. Every language the grammar pack supports gets parsed, on every core when there are many files.
- Text is in an SQLite FTS5 trigram index. The index picks the files that might match, then `what` reads only those files for the matching lines.
- Other branches are searched when `find` or `grep` finds nothing in the working tree, or every time with `--branches`. The default branch and every branch not merged into it are searched, local and remote-tracking, except branches with no commit for a year. A branch adds only the files it changed, and only where its version differs from the working tree. Each file version is indexed once, by its git blob id, so the index grows with the real changes and not with the number of branches. `grep` on branches reads from git only the files the text index picks. A search reads a few moved branches itself. When many have moved, as on the first search of a repository with a thousand branches, a process in the background reads them and the search says some branches may be missing.
- Installed dependencies are searched when `find` or `show` finds nothing in the repository, or every time with `--deps`. `grep` leaves them out unless given `--deps`. The versions come from `composer.lock`, `package-lock.json`, `uv.lock` and `poetry.lock`, and only direct dependencies are searched unless `--all-deps` is given. A lockfile in a subfolder, such as `include/composer.lock`, is read too. The version searched is the one installed, with a note when it differs from the lockfile. Only their symbols are indexed, in `dependencies.db` beside the repository indexes, once for each package version that every repository shares.
- Deleted files come from `git log --diff-filter=D`. Each one is indexed as it was just before the commit that removed it. They are named `COMMIT^:path`, so `git show` accepts the name.
- Before each search, `what` asks git which files changed and re-reads only those. There is no file watcher.
- Outside a git repository, `what` walks the directory and follows its `.gitignore` files. There are no deleted files.

The index lives in `~/.cache/what/` or `$XDG_CACHE_HOME`. It is a cache, so deleting it only costs a rebuild.

Linux and macOS only. Files over 1MB, binaries, minified files and lock files are indexed by path only.

## Log

Every run writes to `~/.local/state/what/what.log`, or to `$XDG_STATE_HOME/what/what.log` when that is set. The log has one line for each command, with how long it took and how it ended, and a line each time the index reads files again. A command that fails is logged with its traceback, which is the only place a failure of the background build shows.

```
2026-11-26 19:34:48,619 INFO 3186358 what.cli: what grep refresh_history in /home/you/project: ok in 13ms
```

At 5MB the file is moved to `what.log.1` and a new one starts. When the folder can't be written, `what` runs without a log.
