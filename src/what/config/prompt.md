## Code search

`what` is an index of this git repository: every symbol, every line of text, and every file that history deleted. It always matches the working tree, uncommitted edits included. Use it before grep, glob or reading a whole file.

- `what <name>`: where a class, function, method or file is defined. `Banana.bend` picks a method of a class. Options: `--kind class`, `--path src/`.
- `what <file>`: what a file defines, with line ranges. `what src/a.py:45-80` prints those lines.
- `what show <symbol>`: the source of one symbol and not the whole file.
- `--ref <branch, tag or commit>` on `what <name>`, `show`, `find` and `outline` answers at that ref, such as `what show Banana.bend --ref origin/dev`.
- `what callers <symbol>`: the lines that call or reference a symbol, without its definition or comments. Calls it can't be sure of end with `[possible]`.
- `what grep <text>`: text in file contents, each hit under the function or class it sits in. Options: `--regex`, `--path`, `--deleted`, `--commits`, `--branches`.

When nothing in the checkout matches, `what <name>` and `grep` search other unmerged branches and label each hit with its branch. `--branches` searches them every time. Files that git history deleted are found too. They are named `COMMIT^:path`, git's name for the file in the commit before the one that deleted it.
