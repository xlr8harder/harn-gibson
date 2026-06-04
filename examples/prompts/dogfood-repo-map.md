You are in a bare project directory created for harn-gibson event capture.

Build a small Python project from scratch that is especially useful for repo
topology and touched-file visual replay:

1. Initialize a git repository and set local test-only git user.name/user.email.
2. Create a depth-2 project layout with several top-level areas, such as
   `src/`, `tests/`, `docs/`, `scripts/`, and `fixtures/`.
3. Add enough code and text across those areas for line counts to vary visibly.
   Keep file contents small, but make some files clearly taller than others.
4. Implement a standard-library-only feature that reads a fixture and produces
   a repository or task summary through a CLI entry point.
5. Run tests and commands after each meaningful milestone so the capture has
   tool calls, tool output, file edits, touched-file batches, and git status.
6. Make logical commits after the initial scaffold, feature implementation,
   test coverage, documentation, and final cleanup.
7. Introduce one intentional failing test or command against a nested file,
   observe the failure, then fix it and rerun the relevant tests.
8. Finish by printing a file listing, line-count summary, and current git
   status so the replay has repo-map evidence for a depth-2 city view.

Avoid network installs unless they are strictly necessary. Prefer the standard
library and local files. Keep secrets, tokens, and host-specific paths out of
the files you create.
