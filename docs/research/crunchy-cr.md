# Crunchy PGO v5 — `postgrescluster` CRD reference for Cluster Ops (kubectl-only console)

Ground truth: GitHub `CrunchyData/postgres-operator` tag **v5.8.8** (latest v5; repo `main` is already PGO **v6.0.2**, June 2026 — pin to v5 semantics below, and note v5.8's CRD serves **only `v1beta1`**). The published CRD is `config/crd/bases/postgres-operator.crunchydata.com_postgresclusters.yaml` (19,660 lines); Go types under `pkg/apis/postgres-operator.crunchydata.com/v1beta1/`. Crunchy's docs site (access.crunchydata.com) returns 403 to non-browser fetches; everything below was verified directly against the CRD YAML, Go types (kubebuilder markers), and controller code. Local clone used for verification: `/tmp/claude-0/-home-user-Benchmarking/a4df773a-6fac-5a9f-af50-bfdacbeed740/scratchpad/pgo` (Crunchy v5.8.8) and `.../scratchpad/ppg` (Percona fork, main ≈ v2.9.0-dev).

## 0. CRD identity / kubectl addressing

- CRD name: `postgresclusters.postgres-operator.crunchydata.com`; group `postgres-operator.crunchydata.com`; served+storage version `v1beta1` (the only version in all of v5); kind `PostgresCluster`, plural `postgresclusters`, singular `postgrescluster`, **no shortNames**. Status subresource enabled; no scale subresource.
- kubectl patterns the console needs:
  - `kubectl get postgresclusters.postgres-operator.crunchydata.com -n NS -o json`
  - `kubectl patch postgrescluster NAME -n NS --type merge -p '{"spec":{...}}'` (merge patch is safe for most fields; `spec.instances` and `spec.backups.pgbackrest.repos` are `listType=map` keyed by `name`, so server-side apply or strategic-merge-by-key semantics do NOT apply via plain merge patch — merge patch replaces whole arrays; prefer `--type json` with positional ops or re-send full arrays).
  - Annotation triggers: `kubectl annotate postgrescluster NAME -n NS --overwrite <annotation>=<value>`.
- Operator deployment default namespace convention: `postgres-operator` (deployment `pgo`). Feature gates set via `PGO_FEATURE_GATES` env on the operator Deployment (comma list, e.g. `TablespaceVolumes=true`).

## 1. Top-level `spec` (path / type / allowed / default / restart behavior)

| Path | Type | Allowed / constraints | Default | Restart / disruption |
|---|---|---|---|---|
| `spec.metadata.labels`, `spec.metadata.annotations` | map[string]string | — | — | Propagated to all managed objects; pod-template changes roll pods |
| `spec.postgresVersion` | int, **required** | v5.8: 11–18; v5.7: 10–17 | — | Changing it alone does NOT perform a major upgrade — requires `PGUpgrade` CR (see §12) |
| `spec.postGISVersion` | string | free-form, e.g. `"3.4"` | — | Only selects image env `RELATED_IMAGE_POSTGRES_{v}_GIS_{postGISVersion}` when `image` unset; change → new image → rolling restart |
| `spec.image` | string | any image ref | operator env `RELATED_IMAGE_POSTGRES_{ver}` (or `_GIS_` variant) | Rolling restart |
| `spec.imagePullPolicy` | string enum | `Always`, `Never`, `IfNotPresent` | unset (K8s default) | Pod template change → rolling restart |
| `spec.imagePullSecrets` | []LocalObjectReference | — | — | Doc comment: "Changing this value causes all running pods to restart." |
| `spec.port` | *int32 | ≥1024 | **5432** | Changes container port/config → rolling restart; services follow |
| `spec.paused` | *bool | — | unset(false) | **Suspends reconciliation/rollout of spec changes.** DB stays up. Condition `Progressing=False, reason=Paused` |
| `spec.shutdown` | *bool | — | unset(false) | **Scales instances + pgBouncer to 0, suspends backup CronJobs.** Services/PVCs remain. Un-set/false to start; startup order uses `status.startupInstance` |
| `spec.standby` | object | see §8 | — | Toggling `enabled` false promotes (timeline switch), true demotes/rebuilds |
| `spec.openshift` | *bool | — | auto-detected | — |
| `spec.disableDefaultPodScheduling` | *bool | — | unset(false=defaults applied) | true = operator's default preferred podAntiAffinity NOT injected; pod template change → rolling restart |
| `spec.supplementalGroups` | []int64 | each 1–2147483647 (no 0/root GID) | — | Pod securityContext → rolling restart |
| `spec.dataSource` | object | see §7 | — | **Bootstrap-only** (honored only before data-initialized condition) |
| `spec.backups` | object | see §6 | **optional since 5.7** (backup-less clusters allowed) | Removing an existing PVC-based repo config requires annotation `postgres-operator.crunchydata.com/authorizeBackupRemoval="true"` (checked in `internal/controller/postgrescluster/pgbackrest.go:3244`) |
| `spec.config` | object | see §9 | — | files: rolling restart; parameters: reload or restart per-GUC |
| `spec.authentication.rules` | []rule, max 10 | 5.8+ only; see §9b | — | pg_hba reload, **no restart** |
| `spec.databaseInitSQL` | `{name, key}` both required | ConfigMap in same namespace | — | Runs **once** post-init via psql; completion recorded in `status.databaseInitSQL` (= ConfigMap name); not re-run on change |
| `spec.customTLSSecret` / `spec.customReplicationTLSSecret` | corev1.SecretProjection | must be set **together**, same `ca.crt`; mounted at `/pgconf/tls` | operator-generated certs | Changing the reference is a pod-template change (rolling restart); rotating contents in-place is picked up via projection update |
| `spec.instances` | []InstanceSet, **required**, minItems 1, listMap key=`name` | see §2 | — | — |
| `spec.users` | []user, max 64, listMap key=`name` | see §3 | one user+db named after the cluster | No restart; SQL applied online |
| `spec.patroni` | object | see §4 | — | — |
| `spec.proxy.pgBouncer` | object | see §5 | — | — |
| `spec.userInterface.pgAdmin` | object | legacy in-cluster pgAdmin 4 (`dataVolumeClaimSpec` required; `config.files/settings/ldapBindPassword`, `image`, `replicas`, `resources`, `service`, scheduling fields) | — | Deployment `<cluster>-pgadmin`; superseded by standalone `pgadmins` CRD |
| `spec.monitoring.pgmonitor.exporter` | object | see §10 | — | Adding/removing/altering exporter restarts instance pods |
| `spec.instrumentation` | object | 5.8+ OpenTelemetry collector sidecar (`image`, `resources`, `config{detectors,exporters,files,environmentVariables}`, `logs{batches,exporters,retentionPeriod}`, `metrics{customQueries{add[],remove[]},exporters,perDBMetricTargets}`) | — | Gated by operator feature gates `OpenTelemetryLogs` / `OpenTelemetryMetrics` (both alpha, default **false**) |
| `spec.service` | ServiceSpec | see §11 | type `ClusterIP` | Applied to the **`<cluster>-ha`** (Patroni leader) Service |
| `spec.replicaService` | ServiceSpec | see §11 | type `ClusterIP` | Applied to `<cluster>-replicas` |

CEL at spec root (5.8.8): `config.parameters.ssl_groups` only allowed when `postgresVersion > 17`.

## 2. `spec.instances[]` (PostgresInstanceSetSpec)

| Path | Type | Constraints | Default | Restart |
|---|---|---|---|---|
| `name` | string | `^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$`; optional when only one set; unique per cluster; `len(cluster)+len(name) ≤ 46` | `""` → controller defaults to zero-padded index (`"00"`, `"01"`…) | Renaming = new StatefulSet (destructive re-create of pods) |
| `replicas` | *int32 | ≥1 | **1** | Scale up/down only |
| `minAvailable` | IntOrString | — | 1 when replicas>1 (PDB) | — |
| `dataVolumeClaimSpec` | VolumeClaimSpec, **required** | CEL: `accessModes` non-empty AND `resources.requests.storage` present | — | Storage size increase → online PVC expand (condition `PersistentVolumeResizing`); shrink impossible |
| `walVolumeClaimSpec` | *VolumeClaimSpec | same CEL | — | Adding/removing moves pg_wal (pods restart) |
| `volumes.temp` | *VolumeClaimSpec | **5.8+** ephemeral temp volume | — | rolling restart |
| `tablespaceVolumes[]` | `{name, dataVolumeClaimSpec}` | name `^[a-z][a-z0-9]*$`; feature gate `TablespaceVolumes` (alpha, default false) | — | rolling restart |
| `resources` | corev1.ResourceRequirements | — | — | rolling restart |
| `containers[]` | []corev1.Container | custom sidecars; feature gate `InstanceSidecars` (alpha, **default true in 5.8**) | — | doc: "causes PostgreSQL to restart" |
| `sidecars.replicaCertCopy.resources` | ResourceRequirements | — | — | rolling restart |
| `affinity` / `tolerations` / `topologySpreadConstraints` / `priorityClassName` | k8s types | — | — | all documented "Changing this value causes PostgreSQL to restart" |
| `metadata.labels/annotations` | maps | — | — | pod template → rolling restart |

Pods/StatefulSets are named `<cluster>-<set>-<4char>`; each Patroni "member" = 1-pod StatefulSet. HA needs either replicas ≥ 2 in one set or ≥ 2 sets. Rolling updates are orchestrated replicas-first, primary last (switchover-based).

## 3. `spec.users[]` (PostgresUserSpec)

- `name` (required): pattern `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, 1–63 chars. Special value `postgres` = manage the superuser's secret (its `databases`/`options` are ignored).
- `databases`: []string (set semantics), each 1–63 chars. DBs are created; removal does NOT drop/revoke.
- `options`: string ≤200 chars, no `;`, CEL forbids `PASSWORD` (case-insensitive) and SQL comments (`--`, `/*`, `*/`). E.g. `"SUPERUSER CREATEDB"`. Ignored for `postgres`.
- `password.type`: enum `ASCII` | `AlphaNumeric`, default **ASCII** (required inside `password` block).
- Max 64 users. Omitting `users` entirely → one default user named after the cluster with a matching database; empty list `[]` → no app users. Removing a user from the list does NOT drop the role.
- Credentials Secret per user: **`<cluster>-pguser-<user>`** with keys `user,password,dbname,host,port,uri,jdbc-uri` (+ `pgbouncer-host/pgbouncer-port/pgbouncer-uri` when pgBouncer enabled). Rotate = delete the `password`/`verifier` data keys from the secret (operator regenerates); set custom = write both `password` and `verifier` (or `verifier:""` to have PGO hash it — per docs). Monitoring secret: `<cluster>-monitoring` (role `ccp_monitoring`).
- Per-cluster annotation `postgres-operator.crunchydata.com/autoCreateUserSchema: "true"` (feature gate `AutoCreateUserSchema`, beta, default true in 5.8) auto-creates a schema per user in each of their databases.

## 4. `spec.patroni`

| Path | Type | Constraints | Default | Restart |
|---|---|---|---|---|
| `dynamicConfiguration` | schemaless object (`x-kubernetes-preserve-unknown-fields`) | Patroni DCS config (e.g. `postgresql.parameters`, `postgresql.pg_hba`, `synchronous_mode`) | — | "reloaded without validation"; individual PG params may force restart (`pending_restart`); operator overrides connection/archive/HA-critical settings |
| `leaderLeaseDurationSeconds` | *int32 | ≥3 | **30** | doc: causes PostgreSQL restart |
| `port` | *int32 | ≥1024 | **8008** | restart |
| `syncPeriodSeconds` | *int32 | ≥1, must be < leaderLeaseDuration | **10** | restart |
| `logging.storageLimit` | Quantity, required-in-block | min 25MB | — | 5.8+ |
| `logging.level` | *string enum | `CRITICAL,ERROR,WARNING,INFO,DEBUG,NOTSET` | `INFO` | 5.8+ |
| `switchover.enabled` | bool, **required in block** | — | — | gate for switchover machinery |
| `switchover.type` | string enum | `Switchover` \| `Failover` | **`Switchover`** | `Failover` is "last resort" |
| `switchover.targetInstance` | *string | must be an observed instance (StatefulSet) name, e.g. `hippo-00-abcd`; **required when type=Failover**, optional for Switchover (healthy replica auto-selected) | — | — |

**Switchover procedure (confirmed in `internal/controller/postgrescluster/patroni.go: reconcilePatroniSwitchover`):**
1. Ensure `spec.patroni.switchover.enabled: true` (+ type/targetInstance as needed).
2. `kubectl annotate postgrescluster NAME --overwrite postgres-operator.crunchydata.com/trigger-switchover="$(date +%s)"` — any value **different from `status.patroni.switchover`** triggers.
3. Completion: `status.patroni.switchover` == annotation value (operator also tracks `status.patroni.switchoverTimeline` for idempotency; timeline change ⇒ done).
4. Preconditions enforced: >1 running instance; Failover requires targetInstance; errors are returned/logged (no Event yet — TODOs in code).
5. Recovery from a stuck attempt: set `switchover.enabled: false` (clears `status.patroni.switchover*`), then re-enable and re-annotate.
- DCS is Kubernetes Endpoints; Patroni scope `<cluster>-ha`, DCS config endpoints `<cluster>-ha-config`. `patronictl` works via `kubectl exec` into the `database` container.

## 5. `spec.proxy.pgBouncer` (PGBouncerPodSpec)

| Path | Type | Constraints | Default | Restart (of pgBouncer) |
|---|---|---|---|---|
| `replicas` | *int32 | **≥0** | **1** | scale only |
| `minAvailable` | IntOrString | — | 1 when replicas>1 | PDB |
| `port` | *int32 | ≥1024 | **5432** | restart |
| `image` | string | env `RELATED_IMAGE_PGBOUNCER`; needs pgBouncer ≥1.15 | env | restart |
| `config.global` | map[string]string | pgbouncer.ini `[pgbouncer]` settings (e.g. `pool_mode: transaction`) | — | **auto-reload, no validation** (can brick pgBouncer) |
| `config.databases` | map[string]string | key=client dbname, value=libpq conn string; `*` fallback | single `*` → primary | reload |
| `config.users` | map[string]string | per-user settings | — | reload |
| `config.files` | []corev1.VolumeProjection | mounted under `/etc/pgbouncer`; `pgbouncer.ini` loaded first | — | changing refs restarts; file contents auto-reload |
| `customTLSSecret` | SecretProjection | `tls.crt`,`tls.key`,`ca.crt` PEM | — | restart |
| `resources` | ResourceRequirements | — | — | restart |
| `service` | ServiceSpec (§11) | — | ClusterIP | applied to Service `<cluster>-pgbouncer` |
| `containers[]` | []Container | gate `PGBouncerSidecars` (alpha, default false) | — | restart |
| `sidecars.pgbouncerConfig.resources` | ResourceRequirements | — | — | restart |
| `affinity/tolerations/topologySpreadConstraints/priorityClassName/metadata` | — | — | — | restart (priorityClassName doc says "causes PostgreSQL to restart") |

Deployment/Service/Secret named `<cluster>-pgbouncer`. Remove the whole `proxy` block to tear down. Status: `status.proxy.pgBouncer.{readyReplicas,replicas,postgresRevision}`, condition `ProxyAvailable`.

## 6. `spec.backups` (pgBackRest + snapshots)

`spec.backups.pgbackrest` (PGBackRestArchive):
- `image` (string; env `RELATED_IMAGE_PGBACKREST`), `metadata`.
- `configuration` []corev1.VolumeProjection → mounted `/etc/pgbackrest/conf.d` (put cloud credentials here, e.g. secret with `s3.conf` containing `repo1-s3-key`, `repo1-s3-key-secret`).
- `global` map[string]string → `[global]` section (retention: `repo1-retention-full: "14"`, `repo1-retention-full-type: count|time`; paths: `repo1-path`; encryption: `repo1-cipher-type`; perf: `process-max`, `compress-type`…).
- `jobs`: `{resources, priorityClassName, affinity, tolerations, ttlSecondsAfterFinished (*int32 ≥60)}` — applies to manual + scheduled + replica-create backup Jobs.
- **`repos[]` — required within block, minItems 1, listMap key=name:**
  - `name` (required): pattern `^repo[1-4]` → max 4 repos, fixed names `repo1..repo4`.
  - exactly one backing store: `volume.volumeClaimSpec` (PVC; CEL accessModes+storage) | `s3{bucket,endpoint,region}` (all required) | `gcs{bucket}` | `azure{container}`.
  - `schedules.{full,differential,incremental}`: cron strings (minLength 6, standard K8s cron). Creates CronJobs `<cluster>-repoN-<type>`. Suspended automatically when `shutdown: true`. No native "pause schedules" flag otherwise (console: remove schedule fields to pause).
  - Changing S3/GCS/Azure fields re-runs `stanza-create` (tracked by `status.pgbackrest.repos[].repoOptionsHash`).
- `repoHost`: `{affinity, priorityClassName (doc: changes restart PostgreSQL), resources, tolerations, topologySpreadConstraints, sshConfigMap (deprecated), sshSecret (deprecated — mTLS now)}`. Dedicated repo-host StatefulSet `<cluster>-repo-host` exists when any `volume` repo is defined.
- **`manual`**: `{repoName (required, ^repo[1-4]), options []string}` (e.g. `--type=full`; don't pass `--repo`, use repoName).
- **`restore`** (in-place): `{enabled *bool (default false)}` + inline PostgresClusterDataSource: `repoName` (required), `options []string` (PITR: `--type=time`, `--target="2026-07-09 14:15:11-04"`, `--set`), `clusterName`/`clusterNamespace` (restore from **another** cluster's repos in-place), `resources`, `affinity`, `tolerations`, `priorityClassName`.
- `sidecars.pgbackrest.resources`, `sidecars.pgbackrestConfig.resources`.

**Manual backup trigger (confirmed `pgbackrest.go: reconcileManualBackup`):**
1. Ensure `spec.backups.pgbackrest.manual.repoName` set.
2. `kubectl annotate postgrescluster NAME --overwrite postgres-operator.crunchydata.com/pgbackrest-backup="$(date +%s)"`.
3. Job runs when annotation value != `status.pgbackrest.manualBackup.id`; needs a **writable primary**. A finished Job with an old ID is deleted when a new ID is annotated (in-flight jobs are allowed to finish first).
4. Observe: `status.pgbackrest.manualBackup{id,active,succeeded,failed,finished,startTime,completionTime}`; condition type `ManualBackupSuccessful` (True/False, reasons `ManualBackupComplete`/`ManualBackupFailed`).

**In-place restore trigger (confirmed `cluster.go: reconcileDataSource` line 349):**
1. Set `spec.backups.pgbackrest.restore = {enabled: true, repoName: repoN, options: [...]}`.
2. `kubectl annotate postgrescluster NAME --overwrite postgres-operator.crunchydata.com/pgbackrest-restore="$(date +%s)"` — fires when annotation != `status.pgbackrest.restore.id` AND `restore.enabled==true`.
3. Operator tears down instances (condition `PGBackRestRestoreProgressing`, reason `ReadyForRestore`), runs restore Job, re-bootstraps.
4. Afterwards set `restore.enabled: false` (or remove block) so future annotation edits can't wipe data.
- `status.pgbackrest`: also `repos[]{name,bound,volume,stanzaCreated,replicaCreateBackupComplete,repoOptionsHash}`, `repoHost.ready`, `scheduledBackups[]{cronJobName,repo,type,startTime,completionTime,active,succeeded,failed}`, `restore` (JobStatus like manualBackup).

`spec.backups.snapshots.volumeSnapshotClassName` (required in block, minLength 1): VolumeSnapshots of pgdata; feature gate `VolumeSnapshots` (alpha, default false).

## 7. `spec.dataSource` (bootstrap-only union; also used for clone/standby bootstrap)

Only consulted before the cluster's `PostgresDataInitialized` condition is true; changing it later is a no-op (in-place restore is the day-2 path). If both `postgresCluster` and `pgbackrest` are set, `postgresCluster` wins (no rejection — comment in `cluster.go:315`).

- **`dataSource.postgresCluster`** (clone/restore from an existing cluster's pgBackRest repo): `clusterName` (default: the new cluster's own name — enables "recreate from own repo"), `clusterNamespace` (default same ns), `repoName` (**required**, `^repo[1-4]`), `options []string` (PITR flags), `resources`, `affinity`, `tolerations`, `priorityClassName`.
- **`dataSource.pgbackrest`** (bootstrap straight from a cloud repo, no source CR needed): `stanza` (default **"db"**), `repo` (single PGBackRestRepo, **required**; CEL: `!has(self.repo.volume)` — **S3/GCS/Azure only**), `configuration []VolumeProjection`, `global map[string]string`, `options []string`, plus Job scheduling fields as above.
- **`dataSource.volumes`** (adopt existing PVCs, e.g. PGO v4 migration): `pgDataVolume{pvcName required, directory optional}`, `pgWALVolume{...}` (must be accompanied by pgData), `pgBackRestVolume{...}`. When `directory` set, a move Job renames the old directory layout.
- Standby bootstrap is NOT here — `spec.standby` is top-level (§8).
- Percona-relevant validation: none of the Percona `PerconaPGBackup/PerconaPGRestore` machinery exists here; everything is spec+annotation driven.

## 8. `spec.standby`

- `enabled` bool — default **true** when block present (kubebuilder default). `repoName` (`^repo[1-4]`) = follow WAL from a pgBackRest repo; `host` + `port` (*int32 ≥1024) = streaming replication from an external primary. Requires **host or repoName**, else Warning event `InvalidStandbyConfiguration` and reconcile is blocked (`controller.go:140-149`).
- Promote: set `enabled: false` (or delete block). Demote a live primary to standby is supported (repoName/host must point at the true primary — split-brain risk is on the user).

## 9. `spec.config` (PostgresConfigSpec)

- `config.files` []corev1.VolumeProjection → mounted under **`/etc/postgres`** (certs for LDAP, extra conf, etc.). Pod-template change → rolling restart. (In ≤5.7 the type was `PostgresAdditionalConfig` with only `files` — path identical.)
- `config.parameters` map[string]{int|string} — **5.8+ only**, max 50 entries. First-class PostgreSQL GUCs (per-GUC reload vs restart applies; Patroni handles `pending_restart`). CEL-**forbidden** keys (exact messages in `postgres_types.go`): `config_file`, `data_directory`, `external_pid_file`, `hba_file`, `ident_file`, `listen_addresses`, `port` (use `spec.port`), `ssl*` (except `ssl_groups` pg18+/`ssl_ecdh_curve`), `unix_socket_*`, `wal_log_hints`, `archive_mode`, `archive_command`, `restore_command`, `recovery_target*`, `hot_standby`, `synchronous_standby_names`, `primary_conninfo`, `primary_slot_name`, `recovery_min_apply_delay`, `cluster_name`, `logging_collector`, `log_file_mode`; `wal_level` only `logical` (replica is implicit). On ≤5.7, set parameters via `patroni.dynamicConfiguration.postgresql.parameters` instead (no validation).

### 9b. `spec.authentication.rules[]` (5.8+)
- Max 10 rules, evaluated in order ahead of the default scram fallback; each is either `{hba: "<raw pg_hba line>"}` (≤100 printable chars, cannot start with `include`) **or** structured `{connection (required, e.g. host/hostssl/hostgssenc), method (required, ≠trust), databases[] ≤20, users[] ≤20, options ≤20 (CEL enforces ldap/radius required options)}`. Reloaded without restart.

## 10. `spec.monitoring.pgmonitor.exporter`

- `image` (env `RELATED_IMAGE_PGEXPORTER`), `resources` (doc: restart), `configuration []VolumeProjection` (custom `queries.yml` → loaded via `extend.query-path`; doc: restart), `customTLSSecret` (exporter web TLS).
- Effect: `exporter` sidecar (postgres_exporter, port 9187) added to every instance pod → enabling/disabling monitoring is a rolling restart. Monitoring role `ccp_monitoring`, secret `<cluster>-monitoring`.
- Annotation `postgres-operator.crunchydata.com/postgres-exporter-collectors: "None"` disables default collectors. Feature gate `AppendCustomQueries` (alpha, default false) appends instead of replaces queries.
- `status.monitoring.exporterConfiguration` = revision hash.

## 11. `spec.service` / `spec.replicaService` (ServiceSpec — same shape everywhere: cluster, replicas, pgBouncer.service, pgAdmin.service)

- `type`: enum `ClusterIP|NodePort|LoadBalancer`, default **ClusterIP**.
- `nodePort` *int32 — rejected at reconcile time with error "NodePort cannot be set with type ClusterIP" (`cluster.go:264`).
- `metadata.labels/annotations`; `ipFamilyPolicy` enum `SingleStack|PreferDualStack|RequireDualStack`; `ipFamilies` [`IPv4`,`IPv6`]; `internalTrafficPolicy`/`externalTrafficPolicy` enum `Cluster|Local`.
- **Important wiring:** `spec.service` is applied to the **Patroni leader Service `<cluster>-ha`** (`patroni.go:225,254`). `<cluster>-primary` is always a **headless** Service whose Endpoints resolve to `<cluster>-ha`'s ClusterIP — it never gets a NodePort/LB itself. So "expose primary externally" = set `spec.service.type`. `spec.replicaService` shapes `<cluster>-replicas`. Other constant Services: `<cluster>-pods` (headless, pod identity), `<cluster>-pgbouncer`.

## 12. Related CRDs / annotations / feature gates (same group)

- **`pgupgrades.postgres-operator.crunchydata.com`** (kind PGUpgrade): `spec{postgresClusterName (required), fromPostgresVersion 11–18, toPostgresVersion 11–18, image, imagePullPolicy, jobs, transferMethod enum {Clone,Copy,CopyFileRange,Link}, resources/affinity/tolerations/priorityClassName}`. Procedure: cluster must be `shutdown: true` and annotated `postgres-operator.crunchydata.com/allow-upgrade: "<pgupgrade-name>"`; after Job success set `spec.postgresVersion` to the new major, un-shutdown.
- **`pgadmins`** (standalone pgAdmin), **`crunchybridgeclusters`** — out of scope.
- Annotation catalog (all prefix `postgres-operator.crunchydata.com/`, from `internal/naming/annotations.go`): `trigger-switchover`, `pgbackrest-backup`, `pgbackrest-restore`, `authorizeBackupRemoval` (="true"), `autoCreateUserSchema` (="true"), `postgres-exporter-collectors` (="None"), `pgbackrest-ip-version` (="IPv6"), `pgbackrest-cloud-log-volume` (=PVC name), `allow-upgrade` (=PGUpgrade name; defined in `internal/controller/pgupgrade/pgupgrade_controller.go:29`), plus internal ones (`finalizer`, `pgbackrest-hash`, `pgbackrest-config`, `pgbackrest-backup-job-completion`).
- Feature gates (`PGO_FEATURE_GATES`, v5.8.8 `internal/feature/features.go`): `AppendCustomQueries=false(alpha)`, `AutoCreateUserSchema=true(beta)`, `AutoGrowVolumes=false(alpha)`, `InstanceSidecars=true(alpha)`, `OpenTelemetryLogs=false(alpha)`, `OpenTelemetryMetrics=false(alpha)`, `PGBouncerSidecars=false(alpha)`, `PGUpgradeCPUConcurrency=true(beta)`, `TablespaceVolumes=false(alpha)`, `VolumeSnapshots=false(alpha)`.

## 13. Status paths the console should watch

- `status.conditions[]`: types `PersistentVolumeResizing`, `PersistentVolumeResizeError` (5.8+), `Progressing`, `ProxyAvailable`, plus pgBackRest conditions (`ManualBackupSuccessful`, `PGBackRestRestoreProgressing`, repo conditions).
- `status.instances[]{name,replicas,readyReplicas,updatedReplicas,desiredPGDataVolume}`; `status.observedGeneration`; `status.startupInstance`/`startupInstanceSet`; `status.postgresVersion` (post-upgrade record); `status.patroni.{systemIdentifier,switchover,switchoverTimeline}`; `status.pgbackrest.*` (§6); `status.proxy.pgBouncer.*`; `status.monitoring.exporterConfiguration`; `status.databaseInitSQL`; `status.usersRevision`/`databaseRevision`.

## 14. v5 version gating (5.7 vs 5.8 — verified by tag diff v5.7.9→v5.8.8)

- 5.8 added: `spec.config.parameters` (5.7 `spec.config` had only `files`), `spec.authentication` (pg_hba rules), `spec.instrumentation` (OTel), `spec.patroni.logging`, `spec.instances[].volumes.temp`, condition `PersistentVolumeResizeError`; `postgresVersion` bounds 10–17 → 11–18.
- Already in 5.7: optional `spec.backups` (+`authorizeBackupRemoval`), everything else in this doc.
- Public v5 tags only exist for 5.7.9 and 5.8.5–5.8.8 (branches `REL_5_7`, `REL_5_8`); earlier 5.x source is not tagged publicly. Console detection: read the operator Deployment image tag, or CRD `spec.versions` won't help (always v1beta1) — better probe: `kubectl explain postgrescluster.spec.config.parameters` succeeds only on 5.8+ CRDs.

## 15. Differences vs Percona Operator for PostgreSQL v2 (fork mapping for the abstraction layer)

Percona v2 wraps a vendored copy of these exact Crunchy types (`pkg/apis/upstream.pgv2.percona.com/v1beta1`) and converts `PerconaPGCluster` → Crunchy `PostgresCluster` in `ToCrunchy()` (`perconapgcluster_types.go`), so an abstraction layer can share ~80% of the schema. Verified mappings (Percona main ≈ v2.9.0-dev):

| Concept | Crunchy PGO v5 (`postgres-operator.crunchydata.com/v1beta1`, kind PostgresCluster) | Percona v2 (`pgv2.percona.com/v2`, kind PerconaPGCluster) |
|---|---|---|
| Stop cluster (scale to 0) | `spec.shutdown: true` | `spec.pause: true` (ToCrunchy: `Shutdown = Pause`) |
| Suspend reconciliation | `spec.paused: true` | `spec.unmanaged: true` (ToCrunchy: `Paused = Unmanaged`) |
| Primary service | `spec.service` (shapes `<cluster>-ha`) | `spec.expose` (ServiceExpose: adds `annotations`,`labels`,`loadBalancerSourceRanges`) |
| Replica service | `spec.replicaService` | `spec.exposeReplicas` |
| TLS | `spec.customTLSSecret` + `spec.customReplicationTLSSecret` | `spec.secrets.customTLSSecret` / `customReplicationTLSSecret` / `customRootCATLSSecret`, plus `spec.tls`, `spec.tlsOnly` |
| Monitoring | `spec.monitoring.pgmonitor.exporter` (postgres_exporter) | `spec.pmm` (PMM client sidecar; querySource pg_stat_monitor/pg_stat_statements); **no pgmonitor block** |
| Extensions | none (image-baked; PostGIS via `postGISVersion`) | `spec.extensions{image, storage, builtin{pg_stat_monitor,pg_audit,pgvector,pg_repack,pg_cron,…}, custom[]}` |
| Manual backup | spec `backups.pgbackrest.manual` + annotation `postgres-operator.crunchydata.com/pgbackrest-backup` | **`PerconaPGBackup` CR** (preferred) or annotation `pgv2.percona.com/pgbackrest-backup` (Percona translates its own prefix to the Crunchy one via `ToCrunchyAnnotation`) |
| Restore | in-place: `backups.pgbackrest.restore.enabled` + annotation `.../pgbackrest-restore` | **`PerconaPGRestore` CR**; annotation `pgv2.percona.com/pgbackrest-restore` internally |
| Version pinning | none | `spec.crVersion` (behavior gated by `CompareVersion`), required-ish; `spec.postgresVersion` min **12** (Crunchy 11) |
| Users | PostgresUserSpec (name/databases/options/password) | same + `secretName` (custom secret name) + `grantPublicSchemaAccess` (pg≥15 CEL) |
| Auto user schema | annotation `.../autoCreateUserSchema` (gate) | first-class `spec.autoCreateUserSchema` (default true ≥2.6.0) |
| Standby | `spec.standby{enabled,repoName,host,port}` | same shape + `maxAcceptableLag` (Quantity, ≥2.9.0) |
| Patroni | `spec.patroni` incl. `switchover` + annotation `.../trigger-switchover` | identical vendored type; Percona validates `dynamicConfiguration...wal_level ∈ {logical,replica}` in code; same Crunchy trigger annotation after translation |
| Backups required? | optional since 5.7 (`authorizeBackupRemoval` to drop) | `spec.backups` historically required; recent versions allow disable via `IsEnabled()`; adds `trackLatestRestorableTime`, VolumeSnapshots with `mode: Online/Offline` + `offlineConfig` |
| Misc Percona-only | — | `spec.initContainer`, `spec.clusterServiceDNSSuffix`, per-component `securityContext`/`initContainer` in its InstanceSets/proxy types |
| Identical passthroughs | `metadata`, `image`, `imagePullPolicy/Secrets`, `port` (default 5432), `openshift`, `dataSource` (same Crunchy type), `databaseInitSQL`, `patroni`, `users` (superset), `instances` (superset), `config` (`files`+`parameters`, Percona ≥2.9), `authentication` (Percona ≥2.9) | — |
- Percona v2 does **not** expose Crunchy's `spec.disableDefaultPodScheduling`, `spec.supplementalGroups`, `spec.userInterface`, `spec.monitoring.pgmonitor` — don't offer those knobs on Percona targets. Conversely `pmm`, `extensions`, `crVersion`, `expose*` don't exist on Crunchy targets.
- Abstraction advice: model operations (switchover, manual backup, in-place restore, shutdown, pause-reconcile, expose, scale, tune-parameters) as verbs; per-backend adapters differ mainly in (a) field paths above, (b) trigger mechanism (Crunchy = spec+same-group annotations; Percona = separate CRs for backup/restore), (c) status locations (Crunchy `status.pgbackrest.manualBackup` vs Percona `PerconaPGBackup.status.state`).

Sources: [Crunchy CRD reference (latest)](https://access.crunchydata.com/documentation/postgres-operator/latest/references/crd), [PostgresCluster 5.0.x CRD page](https://access.crunchydata.com/documentation/postgres-operator/latest/references/crd/5.0.x/postgrescluster), [CrunchyData/postgres-operator @ v5.8.8](https://github.com/CrunchyData/postgres-operator/tree/v5.8.8) (`pkg/apis/postgres-operator.crunchydata.com/v1beta1/*.go`, `config/crd/bases/postgres-operator.crunchydata.com_postgresclusters.yaml`, `internal/naming/annotations.go`, `internal/naming/names.go`, `internal/feature/features.go`, `internal/controller/postgrescluster/{patroni.go,pgbackrest.go,cluster.go,controller.go}`, `internal/controller/pgupgrade/pgupgrade_controller.go`), [in-place restore examples (GH issue #3196)](https://github.com/CrunchyData/postgres-operator/issues/3196), [percona/percona-postgresql-operator (main)](https://github.com/percona/percona-postgresql-operator) (`pkg/apis/pgv2.percona.com/v2/perconapgcluster_types.go`, `pkg/apis/upstream.pgv2.percona.com/v1beta1/postgres_types.go`, `percona/naming/annotations.go`).

<!-- MACHINE_CATALOG -->
```json
{"crd":{"group":"postgres-operator.crunchydata.com","version":"v1beta1","kind":"PostgresCluster","plural":"postgresclusters","singular":"postgrescluster","shortNames":[],"statusSubresource":true,"sourceTag":"v5.8.8"},"annotations":{"switchover":"postgres-operator.crunchydata.com/trigger-switchover","manualBackup":"postgres-operator.crunchydata.com/pgbackrest-backup","inPlaceRestore":"postgres-operator.crunchydata.com/pgbackrest-restore","authorizeBackupRemoval":"postgres-operator.crunchydata.com/authorizeBackupRemoval","autoCreateUserSchema":"postgres-operator.crunchydata.com/autoCreateUserSchema","exporterCollectors":"postgres-operator.crunchydata.com/postgres-exporter-collectors","allowUpgrade":"postgres-operator.crunchydata.com/allow-upgrade"},"fields":[{"path":"spec.postgresVersion","type":"integer","required":true,"min":11,"max":18,"notes":"5.7.x: 10..17; major upgrade requires PGUpgrade CR"},{"path":"spec.postGISVersion","type":"string","required":false},{"path":"spec.image","type":"string","default":"env RELATED_IMAGE_POSTGRES_{v}[_GIS_{gis}]","restart":"rolling"},{"path":"spec.imagePullPolicy","type":"enum","enum":["Always","Never","IfNotPresent"]},{"path":"spec.imagePullSecrets","type":"[]LocalObjectReference","restart":"all pods"},{"path":"spec.port","type":"int32","default":5432,"min":1024,"restart":"rolling"},{"path":"spec.paused","type":"bool","semantics":"suspend reconciliation; condition Progressing=False reason=Paused"},{"path":"spec.shutdown","type":"bool","semantics":"scale workloads to 0, suspend CronJobs"},{"path":"spec.standby.enabled","type":"bool","default":true},{"path":"spec.standby.repoName","type":"string","pattern":"^repo[1-4]"},{"path":"spec.standby.host","type":"string"},{"path":"spec.standby.port","type":"int32","min":1024},{"path":"spec.disableDefaultPodScheduling","type":"bool","restart":"rolling"},{"path":"spec.supplementalGroups","type":"[]int64","itemMin":1,"itemMax":2147483647,"restart":"rolling"},{"path":"spec.databaseInitSQL","type":"{name,key}","required":["name","key"],"semantics":"runs once; status.databaseInitSQL=configmap name"},{"path":"spec.customTLSSecret","type":"SecretProjection","pairedWith":"spec.customReplicationTLSSecret"},{"path":"spec.instances","type":"[]object","required":true,"minItems":1,"listMapKey":"name"},{"path":"spec.instances[].name","type":"string","pattern":"^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$","default":"index %02d","constraint":"len(cluster)+len(name)<=46"},{"path":"spec.instances[].replicas","type":"int32","default":1,"min":1},{"path":"spec.instances[].minAvailable","type":"IntOrString","default":"1 when replicas>1"},{"path":"spec.instances[].dataVolumeClaimSpec","type":"PVCSpec","required":true,"cel":["accessModes nonempty","resources.requests.storage present"]},{"path":"spec.instances[].walVolumeClaimSpec","type":"PVCSpec"},{"path":"spec.instances[].volumes.temp","type":"PVCSpec","since":"5.8"},{"path":"spec.instances[].tablespaceVolumes","type":"[]{name,dataVolumeClaimSpec}","namePattern":"^[a-z][a-z0-9]*$","featureGate":"TablespaceVolumes"},{"path":"spec.instances[].resources","type":"ResourceRequirements","restart":"rolling"},{"path":"spec.instances[].containers","type":"[]Container","featureGate":"InstanceSidecars","restart":"rolling"},{"path":"spec.instances[].affinity|tolerations|topologySpreadConstraints|priorityClassName","restart":"rolling"},{"path":"spec.users","type":"[]object","maxItems":64,"listMapKey":"name","defaultWhenOmitted":"one user+db named after cluster"},{"path":"spec.users[].name","type":"string","required":true,"pattern":"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"},{"path":"spec.users[].databases","type":"[]string(set)","itemMax":63},{"path":"spec.users[].options","type":"string","maxLength":200,"forbidden":["PASSWORD","; ","--","/*"]},{"path":"spec.users[].password.type","type":"enum","enum":["ASCII","AlphaNumeric"],"default":"ASCII"},{"path":"spec.patroni.dynamicConfiguration","type":"object(schemaless)","reload":"auto, no validation"},{"path":"spec.patroni.leaderLeaseDurationSeconds","type":"int32","default":30,"min":3,"restart":"yes"},{"path":"spec.patroni.port","type":"int32","default":8008,"min":1024,"restart":"yes"},{"path":"spec.patroni.syncPeriodSeconds","type":"int32","default":10,"min":1,"restart":"yes"},{"path":"spec.patroni.logging.storageLimit","type":"Quantity","since":"5.8","minHuman":"25MB"},{"path":"spec.patroni.logging.level","type":"enum","enum":["CRITICAL","ERROR","WARNING","INFO","DEBUG","NOTSET"],"default":"INFO","since":"5.8"},{"path":"spec.patroni.switchover.enabled","type":"bool","required":true},{"path":"spec.patroni.switchover.type","type":"enum","enum":["Switchover","Failover"],"default":"Switchover"},{"path":"spec.patroni.switchover.targetInstance","type":"string","requiredWhen":"type=Failover"},{"path":"spec.proxy.pgBouncer.replicas","type":"int32","default":1,"min":0},{"path":"spec.proxy.pgBouncer.port","type":"int32","default":5432,"min":1024,"restart":"pgbouncer"},{"path":"spec.proxy.pgBouncer.config.global","type":"map[string]string","reload":"auto"},{"path":"spec.proxy.pgBouncer.config.databases","type":"map[string]string"},{"path":"spec.proxy.pgBouncer.config.users","type":"map[string]string"},{"path":"spec.proxy.pgBouncer.config.files","type":"[]VolumeProjection","mount":"/etc/pgbouncer"},{"path":"spec.proxy.pgBouncer.service","type":"ServiceSpec"},{"path":"spec.backups.pgbackrest.repos","type":"[]object","minItems":1,"listMapKey":"name","namePattern":"^repo[1-4]","oneOf":["volume.volumeClaimSpec","s3{bucket,endpoint,region}","gcs{bucket}","azure{container}"]},{"path":"spec.backups.pgbackrest.repos[].schedules.{full,differential,incremental}","type":"cron string","minLength":6},{"path":"spec.backups.pgbackrest.global","type":"map[string]string"},{"path":"spec.backups.pgbackrest.configuration","type":"[]VolumeProjection","mount":"/etc/pgbackrest/conf.d"},{"path":"spec.backups.pgbackrest.jobs.ttlSecondsAfterFinished","type":"int32","min":60},{"path":"spec.backups.pgbackrest.repoHost","type":"object","fields":["affinity","priorityClassName","resources","tolerations","topologySpreadConstraints","sshConfigMap(deprecated)","sshSecret(deprecated)"]},{"path":"spec.backups.pgbackrest.manual.repoName","type":"string","required":true,"pattern":"^repo[1-4]"},{"path":"spec.backups.pgbackrest.manual.options","type":"[]string"},{"path":"spec.backups.pgbackrest.restore.enabled","type":"bool","default":false},{"path":"spec.backups.pgbackrest.restore.repoName","type":"string","required":true,"pattern":"^repo[1-4]"},{"path":"spec.backups.pgbackrest.restore.options","type":"[]string"},{"path":"spec.backups.snapshots.volumeSnapshotClassName","type":"string","required":true,"featureGate":"VolumeSnapshots"},{"path":"spec.dataSource.postgresCluster.repoName","type":"string","required":true,"pattern":"^repo[1-4]"},{"path":"spec.dataSource.postgresCluster.clusterName","type":"string","default":"self"},{"path":"spec.dataSource.pgbackrest.stanza","type":"string","default":"db"},{"path":"spec.dataSource.pgbackrest.repo","type":"PGBackRestRepo","required":true,"cel":"cloud repos only (no volume)"},{"path":"spec.dataSource.volumes.{pgDataVolume,pgWALVolume,pgBackRestVolume}","type":"{pvcName required, directory optional}"},{"path":"spec.monitoring.pgmonitor.exporter","type":"object","fields":["configuration[]","customTLSSecret","image","resources"],"restart":"instance pods"},{"path":"spec.config.files","type":"[]VolumeProjection","mount":"/etc/postgres"},{"path":"spec.config.parameters","type":"map[string]IntOrString","since":"5.8","maxProperties":50},{"path":"spec.authentication.rules","type":"[]rule","since":"5.8","maxItems":10},{"path":"spec.service","type":"ServiceSpec","appliesTo":"<cluster>-ha (leader)","typeEnum":["ClusterIP","NodePort","LoadBalancer"],"typeDefault":"ClusterIP"},{"path":"spec.replicaService","type":"ServiceSpec","appliesTo":"<cluster>-replicas"}],"resourceNames":{"primaryService":"<cluster>-primary (headless alias of -ha)","leaderService":"<cluster>-ha","dcsConfig":"<cluster>-ha-config","replicaService":"<cluster>-replicas","podService":"<cluster>-pods","pgbouncer":"<cluster>-pgbouncer","repoHost":"<cluster>-repo-host","userSecret":"<cluster>-pguser-<user>","monitoringSecret":"<cluster>-monitoring","scheduledBackupCronJob":"<cluster>-repoN-<type>"},"statusPaths":{"switchoverDone":"status.patroni.switchover == annotation value","manualBackup":"status.pgbackrest.manualBackup{id,finished,succeeded,failed}","restore":"status.pgbackrest.restore{id,finished}","conditions":["Progressing","ProxyAvailable","PersistentVolumeResizing","PersistentVolumeResizeError","ManualBackupSuccessful","PGBackRestRestoreProgressing"]},"perconaMapping":{"group":"pgv2.percona.com/v2","kind":"PerconaPGCluster","shutdown":"spec.pause","paused":"spec.unmanaged","service":"spec.expose","replicaService":"spec.exposeReplicas","customTLSSecret":"spec.secrets.customTLSSecret","monitoring":"spec.pmm (no pgmonitor)","manualBackup":"PerconaPGBackup CR / pgv2.percona.com/pgbackrest-backup","restore":"PerconaPGRestore CR / pgv2.percona.com/pgbackrest-restore","usersExtra":["secretName","grantPublicSchemaAccess"],"standbyExtra":["maxAcceptableLag"],"specExtra":["crVersion","initContainer","tls","tlsOnly","extensions","autoCreateUserSchema","clusterServiceDNSSuffix"],"absentVsCrunchy":["disableDefaultPodScheduling","supplementalGroups","userInterface","monitoring.pgmonitor"],"postgresVersionMin":12}}
```
