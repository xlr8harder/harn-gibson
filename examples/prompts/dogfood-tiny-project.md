You are in a bare project directory created for harn-gibson event capture.

Build a small Python command-line project from scratch while making the session
interesting for visual replay:

1. Initialize a git repository and set local test-only git user.name/user.email.
2. Create a tiny package, CLI entry point, README, pyproject, and tests.
3. Implement a useful but small feature, such as a task-list formatter or log
   summarizer, with enough code to touch several files.
4. Run the test suite after each meaningful milestone.
5. Make several logical commits so the capture includes git, file-edit, test,
   command-output, and follow-up activity.
6. Add one intentional failing test or lint issue, observe the failure, then fix
   it and rerun tests.
7. Finish by summarizing the repository structure and current git status.

Avoid network installs unless they are strictly necessary. Prefer the standard
library and local files. Keep secrets, tokens, and host-specific paths out of
the files you create.
