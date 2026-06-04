# Repo Map Capture Fixture

This tiny project exists as a stable workspace for harn-gibson replay tests.
It gives the dogfood renderer a depth-2 repository shape with several
top-level districts, varied line counts, and files that can be touched by
captured events.

The code is deliberately small and standard-library-only. It is not meant to be
installed from this checkout during harn-gibson tests; the files are sampled as
repo topology metadata for cinematic display fixtures.

## Layout

- `src/repo_map/` holds the CLI and summary parser.
- `tests/` covers formatting and missing-file behavior.
- `docs/` gives the docs district enough text to render distinctly.
- `fixtures/` provides a local task list for command examples.
- `scripts/` provides a helper that prints line counts.
