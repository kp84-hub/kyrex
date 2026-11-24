# Applying TUI Rift changes to project files

The TUI edits an isolated project copy. A local merge approval copies the
reviewed bytes back to the original project; it does not transfer the clone's
branches or commits, push GitHub changes, or pull a merged GitHub PR locally.
Those Git operations are separate from applying files.

Previously, Rift change detection used only `git status` and diffs against the
clone's current `HEAD`. An edit followed by a commit became invisible, so
`MergeBack` could return success with zero files copied while the project kept
the old file. A path-level "handled" flag could also hide later edits to a
file that had already been approved once.

Normal Rift clones now record their starting tree in private metadata inside
their own `.git` directory. Committed and uncommitted changes remain visible
against that fixed tree. The metadata is never a tracked project file and no
source branches, index or Git refs are replaced. Unborn Git repositories use
an empty starting tree. Existing clones without the metadata retain their
legacy behavior; copied Git worktree files, symlinked Git directories and
shared commondirs are not newly pinned by this change.

Turn-end fallback review checks the actual destination bytes. Already-applied
files are omitted, while a later committed edit to a previously approved file
is offered again. The original protection for files dirty at session start is
retained. Normal per-file and command-write approval gates are unchanged; this
also does not introduce automatic approval of deletions.

On ordinary cleanup, unapplied changes or an inability to verify the changes
retain a recovery workspace with the existing persistence marker. Startup
sweeping skips that retained workspace. Fully applied clones can still be
removed. This protects file recovery after an unsuccessful merge or early
exit; it is not a Git-history synchronization feature or a guarantee against
an untrappable process kill before cleanup runs.

The regression tests use real temporary Git repositories: commit an edit in
the clone, verify a clean Git status, approve its merge and read the original
project file. They also cover later revisions, new files, first commits,
deletions through the manager's existing merge path, private metadata boundaries,
pre-existing operator work, destination verification and cleanup retention.
