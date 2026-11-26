# Contributing

## Set up

Install [uv](https://docs.astral.sh/uv/) and clone the repository. Then install the package and its development dependencies:

```sh
uv sync
```

## Checks

A change must pass all of these before it merges:

```sh
uv run pytest --cov
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

Add a test for each behaviour change. `--cov` reports the lines no test ran, and fails when the tests run less than 90% of the code. The tests use temporary git repositories and directories. The first run may download tree-sitter grammars for the languages it parses; later runs work offline.

## Pull requests

Keep each pull request to one coherent change. Describe what the change does and why, and include the commands you ran to check it. If the change alters the index format, raise `SCHEMA_VERSION` in `src/what/database.py` so old indexes rebuild.

## Releasing

1. Choose the new version number. The project uses [Semantic Versioning](https://semver.org/). Before 1.0, a minor release may change behaviour.
2. Set `project.version` in `pyproject.toml` to the new number.
3. Move the entries in `CHANGELOG.md` from `Unreleased` to a section for the new version, with today's date.
4. Commit the two files.
5. Tag the commit with `v` and the version, then push the tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

The release workflow runs the tests, builds the sdist and wheel, and creates the GitHub Release with those files attached. The workflow stops if the tag and `pyproject.toml` disagree.
