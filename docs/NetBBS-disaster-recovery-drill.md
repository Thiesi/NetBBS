# NetBBS disaster-recovery drill

A documented procedure for an operator to prove a node's backup/restore
mechanism (`netbbs.backup`, design doc §13.4/§13.10) actually works,
against realistic failure conditions, not just its happy path. Run this
once against a disposable copy of a real node before relying on backups
in production, and again after any NetBSD/pkgsrc upgrade that changes
Python, SQLite, or the filesystem NetBBS runs on.

Everything below can be exercised on any POSIX system; do the final pass
on the actual NetBSD host you operate, since that's the one environment
this project targets and the one place `os.kill(pid, 0)`-based liveness
detection (§13.10) is guaranteed to behave exactly as documented.

## Prerequisites

- A running NetBBS node with real content: at least one board with posts
  and edits, an uploaded file, a linked (or Link-disabled, either is
  fine) node identity, SSH enabled with a generated host key, and a
  custom welcome banner.
- Enough free disk to hold a second full copy of the node's database and
  file area (`netbbs.backup restore` stages a complete copy before
  switching anything into place -- see §13.10).

## 1. Take a real backup

```sh
python -m netbbs.backup create --db netbbs.db --identity-dir netbbs_identity --to /path/to/backup1
```

Confirm `backup1/manifest.json` exists and its `checksums` object lists
the database snapshot, every identity file, the SSH host key, and the
welcome banner.

## 2. Confirm restore refuses against a running node

With the node still running:

```sh
python -m netbbs.backup restore --from /path/to/backup1 --db netbbs.db --identity-dir netbbs_identity
```

Expect a refusal naming the node's PID file (`<db-stem>.pid`, written by
`netbbs.__main__` on startup and removed on clean shutdown). Confirm this
still refuses even if the node has been sitting idle for a while (not
mid-transaction) -- this is exactly the case the pre-issue-#75 restore
could not reliably catch.

Stop the node (`SIGTERM`/`Ctrl+C`) and confirm the PID file is gone.

## 3. Confirm a corrupt or truncated backup is refused before anything live is touched

Make a scratch copy of `backup1` first (`cp -r backup1 backup1-corrupt`),
then corrupt it one way at a time and confirm each is refused, and that
`netbbs.db`/`netbbs_files`/`netbbs_identity`/the SSH host key/the banner
are all byte-for-byte unchanged afterward:

- Truncate the database snapshot: `truncate -s 100 backup1-corrupt/netbbs.db`.
- Flip a byte in a blob under `backup1-corrupt/files/`.
- Edit `manifest.json` to change one recorded checksum without changing
  the file it describes.
- Delete one identity file (e.g. `backup1-corrupt/identity/signing.identity`).

Each should raise before any live path is touched -- verify this
directly (checksum the live database/files before and after each
attempt) rather than trusting the error message alone.

## 4. Confirm missing components are refused, not silently skipped

Remove `backup1-corrupt/files/` entirely (simulating a backup taken
against a node with no file area yet, then restored onto one that has
data) and confirm restoring blobs is simply skipped, not treated as
corruption -- this is the one case that's expected to differ from
step 3, since an absent *optional* artifact is not the same as a
present-but-corrupt one.

## 5. Confirm an interrupted restore is recoverable

This is the step worth taking seriously: restore is a five-artifact
switch, and a real crash could land between any two of them.

1. Start a real restore in the background against a genuinely stopped
   node: `python -m netbbs.backup restore --from backup1 --db netbbs.db --identity-dir netbbs_identity &`
2. Kill it hard, mid-run: `sleep 0.2; kill -9 %1` (adjust the delay so
   the kill lands after staging has started but before the whole
   restore finishes -- a large file area gives more of a window; the
   automated test suite's own `test_restore_backup_recovers_the_
   previous_generation_when_a_switch_step_fails` proves the switch-phase
   rollback logic directly and does not depend on timing).
3. Check `netbbs.db`'s directory for `.netbbs-restore-state.json`. If
   present, it names exactly which staging/rollback directories exist
   and which artifacts were still pending -- this is the "clearly
   identified, not a silent mixture" record the design promises.
   Resolve it by hand (the state file names the rollback directory to
   restore from) or, if the kill landed before any live artifact was
   actually switched, simply delete the state file and staging
   directory and retry the restore from scratch.
4. Re-run the same restore command. It should now either complete
   cleanly or, if the previous attempt's rollback already fully
   recovered the prior generation on its own (the common case for an
   in-process exception rather than a hard kill), just proceed normally
   with no leftover state file at all.

## 6. Complete a real restore and verify full recovery

With the node stopped:

```sh
python -m netbbs.backup restore --from backup1 --db netbbs.db --identity-dir netbbs_identity
```

Note the printed rollback-generation path (not deleted automatically --
remove it yourself once satisfied). Then verify, starting the node
again:

- **Identity continuity**: the startup log's `fingerprint` line matches
  the fingerprint from before the drill began.
- **Transports still authenticate**: connect over every transport this
  node has enabled (Telnet/SSH/web) and confirm a real login succeeds.
  For SSH specifically, confirm the client does **not** show a host-key
  warning -- proof the SSH host key restored correctly, not just that a
  *a* key exists.
- **Local content survived**: the board posts/edits/uploaded file from
  the Prerequisites step are all present and correct.
- **Link resumes correctly** (if Link is enabled): the node reaches its
  configured peers again on its own within one sync pass, with no
  duplicate-identity warnings and no re-bootstrapped (different)
  fingerprint.

## 7. Clean up

Remove the rollback-generation directory(ies) left behind once you're
satisfied, and the scratch `backup1-corrupt` copy from step 3. Neither
is removed automatically -- see `netbbs.backup`'s own module docstring
for why.
