# NetBBS disaster recovery drill

This is a SysOp reference for rehearsing the
[backup and recovery procedure](NetBBS-SysOp-Handbook.md#state-backup-and-recovery).
Run it against **disposable copies** and finish with a rehearsal on your actual
deployment platform. A successful automated test is not evidence that your
service settings, game paths, permissions, and off-site copies are recoverable.

## Prepare

Record the installed version, database and identity paths, service settings,
public node fingerprint, and the SSH host-key identity. Keep the TOML and service
configuration separately: the built-in backup does not capture arbitrary host
configuration.

Use representative state: an account, a board post and edit, an uploaded file,
custom artwork, and saved careers/worlds for the games you offer. Record enough
visible game details to recognize a restored career. Allow space for the archive,
staging, and retained rollback generations.

Stop game sessions and companion services before capture. Identify Voidrunner's
actual save root from the live Backup screen; the CLI's home directory can differ
from the service's ([#555](https://github.com/Thiesi/NetBBS/issues/555)).
War Dialer worlds are owned by the corresponding node database; do not transplant
them into an unrelated node.

**MANUAL — outside NetBBS:** copy a completed archive to your recovery machine
and protect it as a secret. Keep a recovery clone off the public network until
verified. Never run the original and its restored Link identity simultaneously.

## 1. Capture and inspect

Use the service's interpreter and actual paths; all paths below are placeholders:

```sh
python -m netbbs.backup create --db /path/to/netbbs.db --identity-dir /path/to/identity --voidrunner-save-dir /path/to/voidrunner --to /path/to/fresh-backup
```

The destination must not exist. Inspect the coverage output and `manifest.json`:

- the database snapshot, identity files, SSH host key, and configured banners;
- the uploaded content tree and representative file bytes;
- Voidrunner career, previous checkpoint, recovery, and score files when present;
- each War Dialer world's key, source path, ownership, and checksum;
- each door's outbound receipts, and the count of entries left in place;
- optional door installations if you enabled that capture.

A backup with no game component does not recover that game's external data.
Third-party installation copies are not quiesced automatically and are not
automatically restored. Door outbound receipts are captured and restored with the
node, replacing whatever stands beside the restored database; still do not infer
an unrecorded post outcome after recovery, in either direction.

## 2. Rehearse the refusal cases

Use a disposable target with the matching node data, isolated listener ports,
and no public network participation.

First, attempt a restore while that disposable node is running. Expect a refusal
naming active state. Repeat with active game sessions where applicable.
A refusal for a missing destination mapping is not evidence that active-node
protection was reached: supply all required options.

Then stop the disposable node and all games. Make a fresh scratch copy of the
archive for each corruption case:

- truncate its database snapshot;
- alter a blob's bytes without changing its content-addressed filename;
- alter a recorded checksum or remove a checksummed identity file;
- alter/remove a manifested career or War Dialer world;
- add a receipt file the manifest does not list.

Attempt restoration and verify that the target state is unchanged. Use hashes
and read-only inspection rather than trusting the error message alone.
Do not edit your only valid backup.

Optional components and required manifested files are different cases. An archive
that never contained a component cannot restore it. Removing an entire optional
tree is not always diagnosed like corrupting a present file; inspect coverage
and verify actual files during the successful restore.

## 3. Restore a complete archive

Every captured War Dialer world needs a destination keyed by its manifest entry;
Voidrunner needs an explicit save-directory destination when present:

```sh
python -m netbbs.backup restore --from /path/to/backup --db /path/to/restored/netbbs.db --identity-dir /path/to/restored/identity --voidrunner-to /path/to/restored/voidrunner --war-dialer-to 1=/path/to/restored/netbbs.db.doors/war-dialer.db
```

This example assumes one War Dialer world with key `1`. Repeat the mapping for
additional worlds; omit the relevant game flag when its component is absent.
An older archive without War Dialer coverage may refuse a target that already
has worlds. Follow the [door guide](NetBBS-door-guide.md#backup-ownership-and-restore)
for namespace-safe recovery instead of bypassing the ownership check.

The tool validates, stages, switches, and retains the previous generation.
Record the reported rollback directory and any external game rollback locations.

**MANUAL — outside NetBBS:** restore the service/TOML configuration and any
external door installations you captured. Configure the restored game paths,
including `VOIDRUNNER_SAVE_DIR` and any War Dialer override, before launch.
The restore command does not rewrite service environment settings.

## 4. Verify the result as a caller and SysOp

Start the isolated recovery node and check:

- the same node fingerprint and expected SSH host key;
- successful login through every transport you offer;
- the recorded posts, revisions, uploaded bytes, and custom artwork;
- the same Voidrunner pilot and score, and War Dialer crew/world history;
- expected account permissions and moderator settings;
- the effective game paths reported by setup/backup screens.

For a coordinated Link recovery exercise, keep the original stopped, then allow
the recovered node to contact its test peers. Verify catch-up, trust state, and
absence of an unexpected identity change. Record failures as focused issues.

## 5. Rehearse interruption only on disposable state

An interrupted restore can leave staging, rollback directories, and
`.netbbs-restore-state.json`. Keep the node and games stopped while resolving it.
Read the named component paths and determine whether each switch happened.
Preserve the journal and all generations until the state is reconciled; do not
delete them merely to make a later restore start.

Developers can exercise deterministic switch failures through
[backup tests](../tests/test_backup.py). An operator rehearsing a hard process
interruption should do so on an isolated copy with a known-good archive and
record exactly which stage was interrupted. A kill during staging is not proof
of recovery from an interruption during the switch.

## 6. Close the drill

Record the release, host, archive source, components checked, observed outcomes,
and remaining manual steps. Keep the validated backup. Remove disposable
corruption cases and obsolete rollback generations only after confirming their
paths and the restored result.

Repeat after changes to backup formats, game storage, service layout, or the
host's storage/runtime environment. The result should be a recovery procedure
you can follow without remembering this development session.
