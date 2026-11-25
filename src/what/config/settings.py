"""The numbers and lists that tune how what indexes and searches, in one place. The code reads them as settings.NAME."""

# How much a search prints.

# A matching line longer than this is cut. Minified files have lines of 100,000 characters.
MAX_LINE_LENGTH = 180

# How many matching lines one file shows in a text search before the rest are counted.
LINES_PER_FILE = 8

# How many lines of a symbol or file `show` prints before it stops.
SHOW_MAX_LINES = 400

# Limits on slow work.

# How many candidate files a text search reads in all, once it has shown its limit and is only counting the hits it
# leaves out. A pattern with no plain text makes every file a candidate.
COUNT_FILES = 2000

# Branches whose last commit is older than this many days are left out. They are rarely the answer, and a large
# repository has hundreds of them. The default branch is always searched.
BRANCH_MAX_AGE_DAYS = 365

# How many files one batch must hold before they are parsed in other processes, one for each core. Starting the
# processes costs about a second, so a small batch is parsed in this one.
PARALLEL_FILES = 50

# How many git commands run at once while the files of branches are listed.
GIT_THREADS = 8

# How many moved branches a search reads itself. When mroe have moved, a process in the background reads them, and
# the search says some branches may be missing.
BRANCHES_READ_INLINE = 20

# How many deleted files, or files on other branches, a text search reads from git. Each one costs a blob read.
GIT_FILES_READ = 100

# How long `git log -S` may run. It reads every commit, which took 10 seconds on a 33,000 commit repository.
COMMIT_SEARCH_SECONDS = 30

# How many files a directory outside git may have. A bigger directory is likely a home directory or a disk, not
# a project.
MAX_FILES = 50_000

# What goes into the index. The index keeps what it stored under the old values, so raising SCHEMA_VERSION in
# models.py is needed after changing one.

# Files bigger than this are generated or data, and would bloat the index.
MAX_BYTES = 1_000_000

# Files matching these keep their path in the index but not their text or symbols.
SKIPPED_NAMES = ["*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "*.svg", "*.snap"]

# The files of an installed dependency that are parsed for its symbols. A package also holds docs, data and
# built assets, which have nothing to find.
DEPENDENCY_SUFFIXES = (".php", ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")

# Data and markup formats have no definitions to find, so parsing them is wasted time.
NO_SYMBOLS = {"json", "yaml", "toml", "xml", "markdown", "html", "css", "csv", "svg"}

# A parse that takes longer than this is a generated or minified file.
PARSE_TIMEOUT_MS = 2000

# A signature longer than this is cut, so one long parameter list can't fill a search result.
MAX_SIGNATURE_LENGTH = 160

# How work is split into batches.

# How many files are parsed between commits to the index. A search in another
# process waits for at most one batch.
BATCH_SIZE = 200

# How many blobs one `git cat-file --batch` call reads. The whole output is
# held in memory, so this keeps one call to a few megabytes.
BLOBS_PER_CALL = 200
