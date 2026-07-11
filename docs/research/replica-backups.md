# Taking pgBackRest backups "through a replica" (backup-standby) — Percona Operator for PostgreSQL v2 & Crunchy PGO v5

Everything below was verified against primary sources: pgBackRest source (`pgbackrest/pgbackrest` @ main, ~2.59-dev, plus release/2.41 and release-note XML), Crunchy PGO source (`CrunchyData/postgres-operator` tag **v5.8.8**, 2026-06), Percona operator source (`percona/percona-postgresql-operator` @ main ≈ **v2.8.x**, plus release tags v2.5.0–v2.8.0), and Percona docs source (`percona/k8spg-docs` @ main, 2026-06). File paths cited are repo-relative; local clones live under `/tmp/claude-0/-home-user-Benchmarking/a4df773a-6fac-5a9f-af50-bfdacbeed740/scratchpad/{pgbr,pgo,pg-op,k8spg-docs}` if the caller wants to re-grep.

---

## 1. TL;DR — what you actually do

**Both operators:** set the pgBackRest option `backup-standby` in the CR's pgBackRest *global* map and make sure the cluster has at least one running replica:

```yaml
# Percona v2 (kind: PerconaPGCluster, pgv2.percona.com/v2)  — same path in
# Crunchy v5 (kind: PostgresCluster, postgres-operator.crunchydata.com/v1beta1)
spec:
  backups:
    pgbackrest:
      global:
        backup-standby: "y"        # or "prefer" (pgBackRest >= 2.54)
```

That is the entire CR-side change. The operator regenerates the pgBackRest config files (ConfigMap `<cluster>-pgbackrest-config`), the option lands in the `[global]` section of every generated config, and every subsequent backup Job (manual, scheduled, and the initial `replica-create` backup) runs with backup-from-standby semantics. There is **no** `repoN-backup-standby` variant — the option is not repo-indexed in pgBackRest (`section: global`, no `group: repo` in `src/build/config/config.yaml:1161-1175`); it applies to whichever repo a given backup job targets.

**Per-backup (one-off) alternative** — pass it as a CLI option on a single backup instead of globally:
- Percona v2: `PerconaPGBackup.spec.options: ["--backup-standby=y"]` (or `--backup-standby=prefer`, or `--no-backup-standby` to force a primary backup when the global is `y`).
- Crunchy v5: `spec.backups.pgbackrest.manual.options: ["--backup-standby=y"]` + trigger annotation `postgres-operator.crunchydata.com/pgbackrest-backup="$(date)"`.
- Scheduled backups in **both** operators cannot carry extra options (the cron path only injects `--type=full|diff|incr` — `internal/controller/postgrescluster/pgbackrest.go` ~line 3110 in both repos), so scheduled standby backups require the global map setting.

---

## 2. pgBackRest `backup-standby` semantics (option definition, exact)

From `src/build/config/config.yaml:1161` and `src/build/help/help.xml` ("Backup from Standby"):

| Property | Value |
|---|---|
| Option | `backup-standby` |
| Section | `global` (also valid on command line; `bool-like`, so `--backup-standby` == `y`, `--no-backup-standby` == `n`) |
| Type | `string-id`, allow-list: `n`, `prefer`, `y` |
| Default | `n` |
| Valid commands | `backup`, `check` (removed from `stanza-*` commands in pgBackRest **2.38**) |
| `y` | "Standby is **required** for backup" — hard error if no standby found |
| `prefer` | "Backup from standby if available otherwise backup from primary" — added in pgBackRest **2.54.0 (2024-10-21)**, release note "Allow requested standby backup to proceed with no standby" (PR #2378) |
| `n` | Backup from primary only |

Official summary (user guide, `doc/xml/user-guide.xml` "Backup from a Standby" / rendered at https://pgbackrest.org/user-guide.html#standby-backup):
- "pgBackRest can perform backups on a standby instead of the primary. Standby backups require the standby host to be configured and the backup-standby option enabled. **If more than one standby is configured then the first running standby found will be used.**"
- "**Both the primary and standby databases are required** to perform the backup, though the vast majority of the files will be copied from the standby to reduce load on the primary. The hosts can be configured in any order; pgBackRest automatically determines which is the primary and which is the standby" (by connecting to each `pgN` and checking `pg_is_in_recovery()`).
- "pgBackRest creates a standby backup that is **identical to a backup performed on the primary**. It does this by **starting/stopping the backup on the primary host**, copying only files that are replicated from the standby host, then copying the remaining few files from the primary host. This means that logs and statistics from the primary database will be included in the backup."

### What runs where (from `src/command/backup/backup.c`, `src/db/helper.c`, `src/db/db.c`)

1. **Cluster discovery** — `dbGet()` (`src/db/helper.c`): iterates `pg1..pgN` from config, opens a connection to each (local socket or remote protocol), classifies primary vs standby. Unreachable hosts produce `WARN: unable to check pgX: [ErrorType] ...` and are skipped. First standby found wins; two primaries → `DbConnectError "more than one primary cluster found"`.
2. **On the PRIMARY only**: `pg_backup_start()` (with immediate checkpoint if `start-fast=y`; log: `execute backup start: backup begins after the requested immediate checkpoint completes`, then `backup start archive = <WAL>, lsn = <LSN>`); if the archive check needs it, `pg_create_restore_point()` + `pg_switch_wal()` (`dbWalSwitch`, `src/db/db.c:761`); at the end `pg_backup_stop()` and wait for the stop WAL segment to appear in the repo.
3. **Standby replay wait** — `dbReplayWait()` (`src/db/db.c:545`): logs `wait for replay on the standby to reach <LSN>`; polls `pg_last_wal_replay_lsn()` until it passes the backup-start LSN, with timeout = `archive-timeout` (**default 1m**, range 100ms–1d) — the timer **resets while replay is making progress**, so only a stalled standby times out. Then issues `CHECKPOINT` on the standby, waits for the standby's pg_control checkpoint to reach the LSN, and verifies the standby is on the **same timeline** as the primary. Logs `replay on the standby reached <LSN>` on success.
4. **Bulk file copy from the STANDBY**: files matching regex `^((pg_data/(base|global|pg_xact|pg_multixact))|pg_tblspc)/` are copied from the standby (`backup.c:2114`) — i.e. all heap/index data. Everything else — `global/pg_control` (always), config files, stats, logs — is copied **from the primary** by a single dedicated process (queue 0 / client 0), while `process-max` parallel clients copy from the standby (`backup.c:2183-2226`). Compression/checksumming happens source-side, so that CPU also lands on the standby.
5. Backup label/manifest is written to the repo exactly as for a primary backup; the manifest records `option-backup-standby=true`.

### Requirements
- Standby must be a **hot standby able to answer queries** and actively **replaying** (in the operators this means a streaming replica managed by Patroni; the user-guide section literally `depend`s on the streaming-replication section).
- Primary **and** standby must both be reachable from the host executing the backup, and that host writes to the repo — in the operator topology one pgBackRest process (on the repo host or backup job pod) coordinates both over TLS, so the classic "both nodes must see the same repository" requirement is satisfied by construction.
- pgBackRest versions must match closely across hosts (operator images guarantee this).

---

## 3. How the operators execute backups (the machinery your console drives)

Identical architecture in both (Percona v2 is a fork of Crunchy PGO v5); verified in `internal/pgbackrest/config.go`, `internal/controller/postgrescluster/pgbackrest.go`, `cmd/pgbackrest/main.go` of both repos.

**TLS server everywhere:** every Postgres instance pod carries a `pgbackrest` sidecar running `pgbackrest server` (TLS, port **8432**, `internal/pgbackrest/iana.go`), with operator-managed CA/client/server certs; `tls-server-auth <cluster-CN>=*`. The dedicated repo host runs the same server.

**Dedicated repo host:** StatefulSet **`<cluster>-repo-host`** (pod `<cluster>-repo-host-0`).
- **Percona v2 (all versions through 2.8): always created**, even for cloud-only (S3/GCS/Azure) repos — `CreatePGBackRestConfigMapIntent` generates `pgbackrest_repo.conf` whenever a repo host name exists (`internal/pgbackrest/config.go:114-127` in pg fork), and the repo-host reconcile is unconditional. Percona docs ("About backups") say you'll "notice an additional `repo-host` Pod after the cluster creation".
- **Crunchy v5.8: only when at least one `repos[].volume` is defined** (`RepoHostVolumeDefined`, `internal/pgbackrest/util.go:29`); cloud-only clusters get no repo host and use a separate config `pgbackrest_cloud.conf`.

**Generated configs** (ConfigMap `<cluster>-pgbackrest-config`, mounted at `/etc/pgbackrest/conf.d/`):
- `pgbackrest_instance.conf` (instance pods): stanza section `[db]` with **only** `pg1-path/pg1-port/pg1-socket-path` (local); `[global]` has `archive-async=y`, `spool-path`, `log-path=/pgdata/pgbackrest/log`, `repoN-path`, and for volume repos `repoN-host=<cluster>-repo-host-0.<cluster>-pods.<ns>.svc.<domain>`, `repoN-host-type=tls` + cert paths. **The user's `spec.backups.pgbackrest.global` map is merged into `[global]` of every one of these files** — including the instance conf.
- `pgbackrest_repo.conf` (repo host): `[db]` lists **every instance** as `pgN-host=<instanceSTS>-0.<cluster>-pods...`, `pgN-host-type=tls`, `pgN-host-ca/cert/key-file`, `pgN-path`, `pgN-port`, `pgN-socket-path` (`populateRepoHostConfigurationMap`, config.go:448+). This is what makes backup-standby work: the backup process sees all members and classifies them at runtime. Nothing is rewritten on failover — the config is symmetric by design.
- `pgbackrest_cloud.conf` (Crunchy 5.8 cloud backup jobs only): same `pgN-host` list + cloud repo options.

**Backup Jobs** (`generateBackupJobSpecIntent`):
- Volume repo (and **everything** in Percona): job container runs the Go wrapper **`/opt/crunchy/bin/pgbackrest`** with env `COMMAND=backup`, `COMMAND_OPTS="--stanza=db --repo=N [--type=...] [+manual/PerconaPGBackup options]"`, `COMPARE_HASH=true`, `CONTAINER=pgbackrest`, `SELECTOR=<dedicated repo-host pod selector>`. The wrapper (source: `cmd/pgbackrest/main.go`) finds `<cluster>-repo-host-0` by label selector, verifies config hashes, then **`kubectl-exec`s (K8s remotecommand) `pgbackrest backup --stanza=db --repo=N ...` inside the repo-host pod's `pgbackrest` container**, relaying stdout/stderr into the job pod log. So "the backup runs on the repo host", and the repo host's pgBackRest reaches into each instance pod via the TLS protocol (remote processes on the instance pods talk to Postgres via local socket).
- Cloud repo in Crunchy 5.8+: job runs `/bin/pgbackrest backup --stanza=db --repo=N ...` **directly in the job pod** with `pgbackrest_cloud.conf`; backup-standby works identically because the pgN-host list is present there too.
- Triggers: (a) automatic **`replica-create`** backup right after stanza creation (job label `postgres-operator.crunchydata.com/pgbackrest-backup=replica-create`); (b) **manual** — Crunchy: `spec.backups.pgbackrest.manual.{repoName,options}` + annotate `postgres-operator.crunchydata.com/pgbackrest-backup=<value>`; Percona: create a **`PerconaPGBackup`** CR (`pgv2.percona.com/v2`, shortname `pg-backup`) with `spec.pgCluster`, `spec.repoName` (required, pattern `^repo[1-4]`), `spec.options []string`, `spec.containerOptions` — the Percona controller acquires a per-cluster lease (one backup at a time; `AnnotationBackupInProgress`), sets the Crunchy annotation + `Manual` spec internally (`percona/controller/pgbackup/controller.go: startBackup`), and tracks `status.state` (Starting/Running/Failed/Succeeded), `status.jobName`, `status.backupType`, `status.destination`, `status.completed`; (c) **scheduled** — CronJobs per repo/type, options limited to `--type` (Percona additionally auto-creates a PerconaPGBackup object for each scheduled job, `percona/controller/pgcluster/backup.go:192`, with no options field).

**Shipped pgBackRest versions (matters for `prefer` and for exec-error behavior):**
| Operator release | pgBackRest |
|---|---|
| Percona v2.5.0 | 2.53 (**no `prefer`** — setting it → OptionInvalidValue rc=32) |
| Percona v2.6.0 | 2.54.2 (`prefer` OK) |
| Percona v2.7.0 | 2.55.0 |
| Percona v2.8.0 | 2.56.0 |
| Crunchy v5.8.8 default | `crunchy-pgbackrest:ubi9-2.56.0` |

---

## 4. Only one replica / standby lagging / degraded modes

- **Single-pod cluster (no replica) with `backup-standby: "y"`**: every backup job — including the initial `replica-create` job — fails and retries forever with `ERROR: [056]: unable to find standby cluster - cannot proceed`. This exact scenario is codified in Crunchy's e2e test `testing/kuttl/e2e/pgbackrest-backup-standby/` (step 00: 1-replica cluster with `backup-standby: "y"`, step 01: assert job pod logs contain `unable to find standby cluster - cannot proceed`, step 02: scale `replicas: 2` and assert the backup completes). The operators do **no validation and no automatic fallback** — the string `backup-standby` appears nowhere in either operator's Go code; it's passed through verbatim.
- **Console preflight rule**: before enabling `"y"`, require ≥1 ready pod with label `postgres-operator.crunchydata.com/role=replica` (leader is `role=master`); otherwise offer `"prefer"` (if image pgBackRest ≥2.54) or refuse.
- **`prefer` degraded path**: backup proceeds from the primary and logs `WARN: unable to find a standby to perform the backup, using primary instead` (`backup.c:207`). WARN is visible in job pod logs at default log level.
- **Standby up but lagging/stalled**: backup start succeeds on primary, then `dbReplayWait` times out after `archive-timeout` (default **1m**; timer resets while replay progresses) with `ERROR: [082]: timeout before standby replayed to <LSN> - only reached <LSN>` + `HINT: is replication running and current on the standby?` + `HINT: disable the 'backup-standby' option to backup directly from the primary.` Related: `[082] timeout before standby checkpoint lsn reached <LSN>`, `[082] unable to query replay lsn on the standby ... HINT: Is this a standby?` (standby promoted mid-backup), `[058] standby is on timeline X but expected Y` (DbMismatchError).
- **Offline backup** (`--no-online`): `WARN: option backup-standby is enabled but backup is offline - backups will be performed from the primary` (auto-degrade; `backup.c:187-195`).
- **Official rationale for `y` being strict** (pgBackRest FAQ, `doc/xml/faq.xml` §"backup-standby"): switching to the primary when the standby is down "often defeats the point" of reducing primary load; "if you really need a backup ... this can be overridden on the command line with `--no-backup-standby`, so there is no need to reconfigure for a one-off backup."

### Error-code reference (exit code == pgBackRest error code, `src/build/error/error.yaml`)
| rc | Error | Message you'll see | Typical cause in this context |
|---|---|---|---|
| 31/32 | OptionInvalid/OptionInvalidValue | invalid value for `backup-standby` | `prefer` on pgBackRest ≤2.53 (Percona ≤2.5.0) |
| 37 | OptionRequired | `backup command requires option: stanza` | hand-run `pgbackrest backup` in a pod without `--stanza=db` (operator configs never set `stanza` in the file) |
| 56 | DbConnect | `unable to find primary cluster - cannot proceed` `HINT: are all available clusters in recovery?` | exec on a replica pod (its config only knows the local standby); or all members in recovery |
| 56 | DbConnect | `unable to find standby cluster - cannot proceed` | `backup-standby=y` with no running standby (single-node, replica down/not ready) |
| 58 | DbMismatch | `standby is on timeline X but expected Y` | promotion/failover during backup |
| 72 | HostInvalid | `backup command must be run on the repository host` | pgBackRest **≤2.54** when `backup` is run on an instance pod whose config has `repoN-host` set (removed in **2.55.0**, release note "Allow backup command to operate on remote repositories") |
| 82 | ArchiveTimeout | `timeout before standby replayed to <LSN> ...` | standby stalled/lagging beyond `archive-timeout` |

---

## 5. How to VERIFY a backup really ran from the standby

Ranked by reliability, all kubectl-only:

1. **Repo metadata (authoritative, always recorded).** Every backup's manifest `[backup]` section and the stanza's `backup.info` `[backup:current]` entry record **`option-backup-standby=true|false`** (`src/info/manifest/serialize.c.inc:66`, `src/info/infoBackup.c:120,338`). For volume repos:
   `kubectl exec <cluster>-repo-host-0 -c pgbackrest -- grep option-backup-standby /pgbackrest/repo1/backup/db/<BACKUP_LABEL>/backup.manifest`
   or `... -- grep -o '"option-backup-standby":[a-z]*' /pgbackrest/repo1/backup/db/backup.info`.
   If the repo is encrypted or cloud-based, use `pgbackrest repo-get` instead of `cat/grep` (it decrypts): `kubectl exec <cluster>-repo-host-0 -c pgbackrest -- pgbackrest --stanza=db --repo=1 repo-get backup/db/backup.info`. **Note: `pgbackrest info --output=json` does NOT expose backup-standby** — don't rely on it.
2. **Job pod logs — but only if you raise the console log level.** Default `log-level-console` is **`warn`** and neither operator overrides it for backup jobs, so the INFO proof lines are invisible by default. Add `--log-level-console=info` (per-backup `options`, or `log-level-console: "info"` in the global map) and `kubectl logs job/<backup-job>` will show: the option echo in `backup command begin 2.x: ... --backup-standby ...`, plus the definitive pair `INFO: wait for replay on the standby to reach <LSN>` / `INFO: replay on the standby reached <LSN>`. At default level you still get: `WARN: unable to find a standby ... using primary instead` (prefer fallback) and all ERRORs (the kuttl test greps job pod logs for the [056] error, proving errors surface there).
3. **Repo-host file logs (INFO by default, no config change needed).** `log-level-file` defaults to `info`; volume-repo commands log to `/pgbackrest/repo1/log/db-backup.log` on the repo host: `kubectl exec <cluster>-repo-host-0 -c pgbackrest -- tail -n 200 /pgbackrest/repo1/log/db-backup.log`.
4. **Live check during a running backup:** on the replica pod, `pg_stat_activity` shows connections with `application_name = 'pgBackRest [backup]'` (set in `dbGetIdx`), and the replica's `pgbackrest` sidecar spawns `pgbackrest --remote-type=pg ... local/remote` worker processes.

There are **no** Kubernetes annotations recording standby-ness; job/PerconaPGBackup status only records success/failure/type/destination.

---

## 6. Why direct `kubectl exec pgbackrest backup` on a replica pod cannot work

1. **The instance pod's config only knows itself.** `pgbackrest_instance.conf` defines exactly one cluster, `pg1` = the local data dir/socket; there are **no `pg2-host` entries and no primary address/credentials anywhere in that container** (`populatePGInstanceConfigurationMap`, config.go:378+ — stanza gets only `pg1-path/pg1-port/pg1-socket-path`). A backup must execute `pg_backup_start/stop` **on the primary**; from a replica pod pgBackRest connects to the local Postgres, finds it in recovery, has no other host to try, and dies with **rc=56**: `unable to find primary cluster - cannot proceed / HINT: are all available clusters in recovery?`. Only the repo host's (or cloud job's) config carries the full `pg1..pgN` membership list.
2. **Repo locality (pgBackRest ≤2.54, i.e. Percona ≤2.6.x images and most Crunchy 5.x images):** for volume repos the instance conf points `repoN-host` at the repo host, and `backup` refuses to run where the repo is remote — **rc=72** `backup command must be run on the repository host`. pgBackRest 2.55.0 (Percona 2.7+/Crunchy 5.8+) lifted this restriction, but you still hit (1).
3. **Missing stanza:** the generated files never set `stanza`, so a bare `pgbackrest backup` → **rc=37** `backup command requires option: stanza`.
4. **Even on the primary pod it's wrong:** if `backup-standby: "y"` is in the global map it is mirrored into the instance conf too, so the exec fails rc=56 `unable to find standby cluster` (no `pg2` configured locally). And a "successful" exec (cloud repo, 2.55+, correct flags) would bypass the operator entirely: no Job/PerconaPGBackup status, no per-cluster backup lease (Percona serializes backups via `AnnotationBackupInProgress`), no config-hash check, and pgBackRest's backup/stanza **locks are host-local**, so an exec'd backup can run concurrently with an operator backup against the same repo — the exact race the operator machinery exists to prevent.
5. **Correct kubectl-only paths for the console:** patch the CR global map; create a `PerconaPGBackup` (Percona) or set `manual` + annotate (Crunchy). Exec'ing `pgbackrest backup --stanza=db --repo=1` **inside `<cluster>-repo-host-0`** does work mechanically (it is literally where the operator runs it) but is unmanaged — use only as a diagnostic, never as the console's backup path.

---

## 7. Performance/load rationale (what to tell users in the UI)

- Bulk I/O (reading every data file), page-checksum validation, and **compression/encryption CPU** happen on the standby; the primary only performs the start/stop checkpoint, an optional restore-point + `pg_switch_wal`, and serves a handful of small non-replicated files (`pg_control`, configs, stats). This protects the primary's page cache and I/O bandwidth during multi-hour full backups of large clusters.
- Costs/caveats: the standby's replay can slow during the copy (it also gets a `CHECKPOINT` issued by pgBackRest); backup network traffic originates from the standby pod; with synchronous replication a saturated standby can back-pressure primary commits; and `y` trades backup availability for primary protection (see FAQ rationale above — that's why `prefer` exists since 2.54).

## 8. Percona-v2-specific notes

- **Percona has no documentation page for backup-standby at all** (zero occurrences in the docs source repo as of 2026-06). The supported knobs are documented generically: `backups.pgbackrest.global` = "Settings, which are to be included in the global section of the pgBackRest configuration generated by the Operator" (docs/operator.md), and `PerconaPGBackup.spec.options` = "command line options supported by pgBackRest" (docs/backup-resource-options.md). So treat the Crunchy kuttl test + pgBackRest docs as the authority; the mechanism is identical because Percona's fork keeps upstream `internal/` code.
- Percona routes **all** repo types through the repo-host exec (`generateBackupJobSpecIntent` has no cloud branch in the fork), and always deploys `<cluster>-repo-host` — so for Percona the answer to "is a repoHost required?" is: it's always there; you never need to add one. For Crunchy 5.8 cloud-only clusters, no repo host exists and none is needed — backup-standby still works from the job pod.
- Version gate for `prefer`: Percona ≥2.6.0 only. Percona ≤2.5.0 (pgBackRest 2.53) supports only `"y"`/`"n"`.
- Percona serializes backups per cluster (lease + in-progress annotation); a second PerconaPGBackup waits. Console should surface queue state from `status.state`.

## 9. Concrete console recipes (kubectl-only)

```bash
# enable globally (Percona; same JSON path with kind postgrescluster for Crunchy)
kubectl -n NS patch perconapgcluster CLUSTER --type merge \
  -p '{"spec":{"backups":{"pgbackrest":{"global":{"backup-standby":"prefer"}}}}}'

# one-off standby backup (Percona)
cat <<EOF | kubectl -n NS apply -f -
apiVersion: pgv2.percona.com/v2
kind: PerconaPGBackup
metadata: { generateName: standby-bkp- }
spec:
  pgCluster: CLUSTER
  repoName: repo1
  options: ["--type=full", "--backup-standby=y", "--log-level-console=info"]
EOF

# verify
kubectl -n NS logs job/$(kubectl -n NS get pg-backup NAME -o jsonpath='{.status.jobName}') \
  | grep -E 'backup command begin|replay on the standby|unable to find (a )?standby'
kubectl -n NS exec CLUSTER-repo-host-0 -c pgbackrest -- \
  grep option-backup-standby /pgbackrest/repo1/backup/db/LABEL/backup.manifest
```

Preflight checklist for the console before allowing `"y"`: (1) ≥1 pod with `postgres-operator.crunchydata.com/role=replica` Ready; (2) `pgbackrest version` on repo host ≥2.54 if offering `prefer`; (3) warn that `archive-timeout` (default 1m) governs the standby-replay wait and can be raised via the same global map; (4) recommend always injecting `--log-level-console=info` into per-backup options so verification from job logs is possible.