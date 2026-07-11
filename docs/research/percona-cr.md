# Percona Operator for PostgreSQL v2 — full user-configurable surface of `perconapgcluster` (pgv2.percona.com/v2)

Verified against the **actual CRD YAML and Go API types at git tag `v2.7.0`** of `github.com/percona/percona-postgresql-operator` (files: `config/crd/bases/pgv2.percona.com_perconapgclusters.yaml`, `pkg/apis/pgv2.percona.com/v2/perconapgcluster_types.go`, `pkg/apis/postgres-operator.crunchydata.com/v1beta1/*.go`, `deploy/cr.yaml`), cross-checked against tags v2.3.0/v2.4.0/v2.5.0/v2.6.0/v2.8.0/v2.9.0 for version gating, and against the Percona docs source repo `percona/k8spg-docs` (`docs/operator.md`, `annotations.md`, `immutable-options.md`, `change-primary.md`, `backups-*.md`, `options.md`, `pause.md`, `scaling.md`, `users.md`, `ReleaseNotes/*`). The docs site (docs.percona.com/percona-operator-for-postgresql/2.0/operator.html) 403-blocks bots; the docs git source was used instead — it is identical content.

## 0. CRD identity and companion CRDs

| Kind | plural | shortName | apiVersion | Purpose |
|---|---|---|---|---|
| PerconaPGCluster | perconapgclusters | **`pg`** | pgv2.percona.com/v2 | the cluster CR |
| PerconaPGBackup | perconapgbackups | **`pg-backup`** | pgv2.percona.com/v2 | on-demand backup trigger + record of every backup (incl. scheduled) |
| PerconaPGRestore | perconapgrestores | **`pg-restore`** | pgv2.percona.com/v2 | in-place restore trigger |
| PerconaPGUpgrade | perconapgupgrades | (none) | pgv2.percona.com/v2 | PostgreSQL **major**-version upgrade |
| PostgresCluster (internal) | postgresclusters | — | postgres-operator.crunchydata.com/v1beta1 (**renamed to `upstream.pgv2.percona.com` in operator 3.0.0**) | Crunchy CR the operator generates 1:1 from the Percona CR; users must not edit it |

PerconaPGCluster printer columns: `Endpoint`=.status.host, `Status`=.status.state (`initializing|paused|stopping|ready|error`), `Postgres`=.status.postgres.ready, `PGBouncer`=.status.pgbouncer.ready, `Age`. Useful status: `.status.postgres.{size,ready,instances[],version,imageID}`, `.status.pgbouncer.{size,ready}`, `.status.patroniVersion` (2.6+), `.status.installedCustomExtensions` (2.6+), `.status.conditions` (2.7+), `.status.host`.

CRD-level **required** spec fields: `backups`, `instances`, `postgresVersion`. CEL validation (2.7+): `grantPublicSchemaAccess=true` on any user requires `postgresVersion >= 15`.

Every PerconaPGCluster is converted to a Crunchy `PostgresCluster` (same name/namespace) via `ToCrunchy()`; **all CR annotations are copied through with prefix rewrite `pgv2.percona.com/ → postgres-operator.crunchydata.com/`** (`percona/naming/prefix.go`). So any Crunchy trigger annotation can be written on the `pg` object either with the crunchy prefix (passed as-is) or with the `pgv2.percona.com/` prefix (rewritten).

## 1. metadata (of the CR)

| Path | Type | Notes |
|---|---|---|
| `metadata.name` | string | ≤22 chars recommended (instance-set name + cluster name combined must be ≤46 chars because of the StatefulSet/label limit); immutable |
| `metadata.finalizers[] = percona.com/delete-pvc` | — | on CR delete: deletes PVCs **and user Secrets** |
| `metadata.finalizers[] = percona.com/delete-ssl` | — | on CR delete: deletes TLS Secrets/cert-manager objects |
| `metadata.finalizers[] = percona.com/delete-backups` | — | 2.6+: deletes all backups from all repos on cluster delete. Tech preview |
| `metadata.annotations["pgv2.percona.com/custom-patroni-version"]` | "3"\|"4" | 2.6–2.7 only: skips the `<cluster>-patroni-version-check` probe Pod. **Deprecated + ignored ≥2.8** |

## 2. Top-level spec

| Path | Type | Allowed / default | Restart impact | Since |
|---|---|---|---|---|
| `spec.crVersion` | string | semver, e.g. `2.7.0`; **defaults to running operator version** if empty. Gates operator behavior (features listed below check `CompareVersion`). Bump after operator upgrade to roll out new behavior; never set higher than the operator | bumping usually triggers rolling update (statefulset template changes) | 2.0 |
| `spec.image` | string | PG container image, e.g. `percona/percona-postgresql-operator:2.7.0-ppg17.5.2-postgres` | **rolling restart of PG pods** | 2.0 |
| `spec.imagePullPolicy` | enum | `Always|Never|IfNotPresent` | pod template change → restart | 2.0 |
| `spec.imagePullSecrets[]` | []LocalObjectReference | — | **restarts all pods** | 2.0 |
| `spec.postgresVersion` | int **required** | 2.3–2.5: 12–16; 2.6–2.7: 12–17 (2.9: up to 18, 13 EOL) | only changed via PerconaPGUpgrade (major upgrade); do not edit directly | 2.0 |
| `spec.port` | int | default 5432, min 1024 | PG restart | 2.0 |
| `spec.pause` | bool | default absent(false). `true` = graceful full shutdown: workloads scaled to 0, CronJobs suspended; Services/PVCs remain. Maps to crunchy `shutdown`. **Refused while a backup Job is running** (delete the backup job first) | stops/starts everything | 2.0 |
| `spec.unmanaged` | bool | `true` = operator stops reconciling this CR (maps to crunchy `paused`); status not updated; use for manual patronictl surgery | none by itself | 2.0 |
| `spec.openshift` | bool | unset = autodetect | — | 2.0 |
| `spec.tlsOnly` | bool | default false; enforce TLS for all connections | PG restart | **2.6.0** |
| `spec.autoCreateUserSchema` | bool | **defaults to `true` when crVersion ≥ 2.6.0** (Default() sets it); creates per-user schemas in each of the user's databases. Internally becomes crunchy annotation `postgres-operator.crunchydata.com/autoCreateUserSchema=true` | none | **2.6.0** |
| `spec.metadata.labels` / `spec.metadata.annotations` | map | applied globally to **all** objects the operator creates | may roll pods (template change) | **2.6.0** (field existed earlier but only honored ≥2.6 in backup/restore/schedule paths) |
| `spec.initContainer` | object | `{image, resources, containerSecurityContext}` — config of the operator-injected init container for the cluster | pod restart | **2.7.0** (K8SPG-613) |

Post-2.7 top-level additions (reject/hide for 2.3–2.7 targets): `spec.tls{certValidityDuration,caValidityDuration,pgBackRestCertValidityDuration}` (2.9, cert-manager only), `spec.config.files` (2.9, LDAP CA mount), `spec.authentication.rules[]{connection,method,users,databases,options,hba}` (2.9, structured pg_hba/LDAP), `spec.clusterServiceDNSSuffix` (2.9), `spec.extensions.storage.{forcePathStyle,disableSSL}` (2.8), `env/envFrom` on instances/pgBouncer/pgbackrest (2.8), `loadBalancerClass` on expose blocks (2.8), `sidecarVolumes/sidecarPVCs` (2.9), `backups.volumeSnapshots{className,mode,schedule}` (2.9), `standby.maxAcceptableLag` (2.9), `dataSource.{apiGroup,kind,name}` VolumeSnapshot bootstrap (2.9).

## 3. `expose` / `exposeReplicas` (Services for primary / replicas)

Type `ServiceExpose` (both sections identical):

| Path | Type | Allowed/default |
|---|---|---|
| `expose.type` | enum | `ClusterIP` (default) \| `NodePort` \| `LoadBalancer` |
| `expose.nodePort` | int32 | valid free node port; only for NodePort/LB |
| `expose.annotations` / `expose.labels` | map | applied to the Service |
| `expose.loadBalancerSourceRanges[]` | []string | CIDRs |
| `expose.loadBalancerClass` | string | **2.8+ only** |

`exposeReplicas` (same shape) exists **since 2.4.0**; creates `<cluster>-replicas` Service. Services created regardless: `<cluster>-ha` (primary), `<cluster>-replicas`, `<cluster>-pgbouncer`. Changing these does not restart pods.

## 4. `standby`

| Path | Type | Notes |
|---|---|---|
| `standby.enabled` | bool | default false. Cluster runs read-only, replaying WAL |
| `standby.repoName` | string `^repo[1-4]` | repo-based standby (reads WAL from shared pgBackRest repo) |
| `standby.host` / `standby.port` | string / int32(min 1024) | streaming-replication standby |
| `standby.maxAcceptableLag` | quantity string | **2.9+** — lag guardrail, sets `StandbyLagging` condition |

Either repoName, host+port, or both (both → pgbackrest method preferred, basebackup fallback). **Promotion** = set `standby.enabled=false` (ensure old primary is dead first — split-brain risk). Demoting an active cluster to standby is destructive. Requires identical `customTLSSecret`/`customReplicationTLSSecret` on both sites.

## 5. `users[]`

| Path | Type | Allowed / default | Notes |
|---|---|---|---|
| `users[].name` | string, req | `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, ≤63 | listMap key. `monitor` is **reserved** (PMM) and ignored. `postgres` entry exposes the superuser secret |
| `users[].databases[]` | []string (set) | PG identifiers | DBs are created; **removal does NOT revoke access / drop DB**; ignored for `postgres` |
| `users[].options` | string | `ALTER ROLE` options except PASSWORD; ≤200 chars; no `;` (pattern `^[^;]*$`); e.g. `"SUPERUSER"`, `"CREATEDB CREATEROLE"`; ignored for `postgres` |
| `users[].password.type` | enum | `ASCII` (default) \| `AlphaNumeric` |
| `users[].secretName` | string | overrides default `<cluster>-pguser-<user>` |
| `users[].grantPublicSchemaAccess` | bool | **2.7.0+**, requires postgresVersion ≥ 15 (CEL-validated) |

Behavior: empty/omitted `users` → default user `<cluster>` with database `<cluster>` (secret `<cluster>-pguser-<cluster>`). Removing a user from the list does not drop the role. Secret keys: `user, password, verifier, host, port, dbname, uri, jdbc-uri` plus `pgbouncer-host, pgbouncer-port, pgbouncer-uri, pgbouncer-jdbc-uri`. **Password rotation:** `kubectl patch secret <cluster>-pguser-<user> -p '{"data":{"password":""}}'` → operator regenerates; setting a custom password = write `password` (operator computes SCRAM `verifier`; in ≤2.7 write both password+verifier — "custom credentials fully respected" landed in 2.8). No pod restart.

## 6. `databaseInitSQL`

`databaseInitSQL.name` (ConfigMap name, required) + `databaseInitSQL.key` (data key, required). Runs **once at cluster bootstrap only** via psql as superuser; adding it to an existing cluster does nothing. Same namespace as CR.

## 7. `dataSource` (bootstrap/clone/migrate — creation-time only, immutable)

Exactly one of three modes (all only honored at cluster creation):

- **`dataSource.postgresCluster`** (clone/PITR from another cluster's repo): `clusterName` (default = new cluster's name), `clusterNamespace` (cross-ns needs cluster-wide operator), `repoName` (required, `^repo[1-4]`), `options[]` (pgbackrest restore CLI, e.g. `--type=time`, `--target="2024-06-09 14:15:11-04"`, `--set=<backupID>`), `resources`, `affinity`, `priorityClassName`, `tolerations[]`. Since 2.5.0 a `pg-restore` CR is auto-created to track the bootstrap restore (annotated `pgv2.percona.com/cluster-bootstrap-restore`).
- **`dataSource.pgbackrest`** (restore directly from a repo, e.g. cloud bucket, no source CR needed): `stanza` (required, usually `db`), `repo{name,s3|gcs|azure|volume}` (required), `configuration[]` (VolumeProjection; secret with `s3.conf` etc.), `global{}` (e.g. `repo1-path`), `options[]`, `resources`, `affinity`, `priorityClassName`, `tolerations[]`.
- **`dataSource.volumes`** (adopt existing PVCs, v1→v2 migration): `pgDataVolume{pvcName(required),directory,tolerations[],annotations,labels}`, `pgWALVolume{...}` (must accompany pgDataVolume), `pgBackRestVolume{...}`. `directory` set → a "move dir" Job renames the data dir.
- 2.9+ adds `dataSource.{apiGroup: snapshot.storage.k8s.io, kind: VolumeSnapshot, name}` for snapshot bootstrap.

## 8. `instances[]` (min 1, listMap key `name`)

| Path | Type | Allowed / default | Restart on change |
|---|---|---|---|
| `name` | string | `^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$`, default `""`→`00`; unique; len(cluster)+len(name) ≤ 46 | new instance set = new STS |
| `replicas` | int32 | default 1, **min 1** | scale only |
| `metadata.labels/annotations` | map | onto instance Pods/STS | rolls pods |
| `resources` | ResourceRequirements | requests/limits cpu+memory for the `database` container | **restart** |
| `dataVolumeClaimSpec` | PVCSpec, **required** | `accessModes[]`, `storageClassName`, `resources.requests.storage`, `resources.limits.storage` | storage grow: auto PVC expansion **since 2.5.0** (StorageClass must AllowVolumeExpansion); `limits.storage` is the cap for the `AutoGrowVolumes` feature gate; shrink impossible |
| `walVolumeClaimSpec` | PVCSpec | separate pg_wal PVC | add/remove triggers data move + restart |
| `tablespaceVolumes[]` | `{name ^[a-z][a-z0-9]*$, dataVolumeClaimSpec}` | **2.4.0+**, requires operator env `PGO_FEATURE_GATES=TablespaceVolumes=true` | restart |
| `affinity` | corev1.Affinity | full node/pod (anti)affinity | **restart** |
| `tolerations[]` | []Toleration | — | **restart** |
| `topologySpreadConstraints[]` | []TSC | — | **restart** |
| `priorityClassName` | string | — | **restart** |
| `securityContext` | PodSecurityContext | **2.4.0+** | **restart** |
| `sidecars[]` | []corev1.Container | full custom sidecars (name/image/command/args/env/envFrom/volumeMounts/securityContext…) | **restart** |
| `containers.replicaCertCopy.resources` | Sidecar | resources for the operator's `replica-cert-copy` sidecar (**2.4.0+**) | restart |
| `initContainers[]` | []corev1.Container | additional custom init containers | **restart** |
| `initContainer` | `{image,resources,containerSecurityContext}` | operator's own init container config (**2.7.0+**, K8SPG-708) | restart |
| `minAvailable` | int-or-string | PDB minAvailable; defaults to 1 when replicas>1 | none |
| `volumeMounts[]` | []VolumeMount | extra mounts into instance pods | **restart** |
| `env[]/envFrom[]` (2.8+), `sidecarVolumes[]/sidecarPVCs[]` (2.9+) | — | not in ≤2.7 | — |

Rolling updates: the operator recreates instance pods one-at-a-time, replicas first, primary last (final step causes one switchover/failover blip).

## 9. `patroni`

| Path | Type | Default / allowed | Restart |
|---|---|---|---|
| `patroni.syncPeriodSeconds` | int32 | **10**, min 1 → Patroni `loop_wait` + probe `periodSeconds` (+ `timeoutSeconds=sync/2`) | **PG restart** |
| `patroni.leaderLeaseDurationSeconds` | int32 | **30**, min 3 → Patroni `ttl` + probe `failureThreshold=lease/sync`. This is the automatic-failover detection window | **PG restart** |
| `patroni.port` | int32 | **8008**, min 1024 | **PG restart** |
| `patroni.dynamicConfiguration` | free-form object (schemaless) | See below | reload; postmaster-context params → automatic rolling restart |
| `patroni.switchover.enabled` | bool | default **false**; must be true to allow switchover/failover | none |
| `patroni.switchover.targetInstance` | string | *instance* (member/STS) name, e.g. `cluster1-instance1-bmdp`; optional for Switchover (random healthy replica picked), **required for Failover** | — |
| `patroni.switchover.type` | enum | `Switchover` (default) \| `Failover` (last resort, forces target) | — |
| `patroni.createReplicaMethods[]` | []enum `basebackup|pgbackrest` | **2.7.0+**; order = priority; note: after the first backup the operator itself puts `pgbackrest` first; changes need a manual Patroni reload/pod restart to propagate | none automatic |

`dynamicConfiguration` semantics (verified in `internal/patroni/config.go DynamicConfiguration()` + docs `immutable-options.md`): only **`postgresql.parameters`** (map param→value) and **`postgresql.pg_hba`** (list of raw hba lines, appended **after** operator-mandatory lines; when absent, defaults appended) are effectively honored. The operator force-overrides: `ttl`, `loop_wait` (from the two spec fields), `postgresql.use_slots=false`, `postgresql.use_pg_rewind=true` (PG>10), `standby_cluster` (from `spec.standby`). Other DCS keys (`synchronous_mode`, `maximum_lag_on_failover`, …) are ignored/overwritten on reconcile. Parameters the operator **enforces and reverts**: `ssl*`, `unix_socket_directories`, `log_file_mode`, `archive_mode=on`, `archive_command` (pgbackrest archive-push), and appends to `shared_preload_libraries` per enabled builtin extensions (user values are loaded ahead of mandatory ones). Overridable defaults: `wal_level` (default **logical**), `jit=off`, `password_encryption=scram-sha-256`, `archive_timeout=60s`, `huge_pages` (computed), `restore_command` (default `pgbackrest --stanza=db archive-get %f "%p"`). No validation is performed — bad values can take the cluster down. Config is applied via `patronictl edit-config --replace=-`, pending restarts via `patronictl restart --pending --force` (automatic rolling restart for postmaster-context params). Escape hatch (2.7+): annotate the generated Patroni ConfigMap with `percona.com/override-config` to stop the operator reconciling it (K8SPG-712).

### Switchover / failover procedure (kubectl-only)
1. `kubectl patch pg <cluster> --type=merge -p '{"spec":{"patroni":{"switchover":{"enabled":true,"targetInstance":"<instance-name-or-empty>"}}}}'` (add `"type":"Failover"` for forced failover — targetInstance then mandatory).
2. Trigger: `kubectl annotate --overwrite pg <cluster> postgres-operator.crunchydata.com/trigger-switchover="$(date)"` — the annotation **value change** is the trigger; operator execs `patronictl switchover --scheduled=now --force [--candidate=X]` (or `patronictl failover --force`). Completion is recorded in the crunchy `postgrescluster` `.status.patroni.switchover` (== annotation value when done).
3. Afterwards set `switchover.enabled=false`. Role labels to watch: pods labeled `postgres-operator.crunchydata.com/role=master|replica` (and `instance`/`instance-set` labels).

## 10. `proxy.pgBouncer`

Percona **always deploys pgBouncer by default** (spec defaulting creates the section; replicas default 1). Disable with `proxy.pgBouncer.replicas: 0`.

| Path | Type | Default / allowed | Restart of pgBouncer |
|---|---|---|---|
| `replicas` | int32 | **1**, min **0** | scale |
| `image` | string | e.g. `percona/percona-pgbouncer:1.24.1` (≥2.7; older: operator image `...-pgbouncer`); or env `RELATED_IMAGE_PGBOUNCER` | **restart** |
| `port` | int32 | **5432**, min 1024 | **restart** |
| `exposeSuperusers` | bool | false — allow SUPERUSER logins through pgBouncer | config reload |
| `config.global` | map[string]string | any pgbouncer.ini `[pgbouncer]` settings (e.g. `pool_mode: transaction`); **hot-reloaded, no validation** | none (reload) |
| `config.databases` | map | `[databases]` entries; key = client dbname, value = libpq conn string; `*` fallback (default routes to primary) | reload |
| `config.users` | map | `[users]` per-user settings | reload |
| `config.files[]` | []VolumeProjection | files mounted under `/etc/pgbouncer`; `pgbouncer.ini` loaded first | reference change → restart; content changes reload |
| `customTLSSecret` | SecretProjection | tls.crt/tls.key/ca.crt for client-side TLS | **restart** |
| `expose` | ServiceExpose | `type` ClusterIP(default)/NodePort/LoadBalancer, annotations, labels, loadBalancerSourceRanges, nodePort | none |
| `resources` | ResourceRequirements | — | **restart** |
| `affinity` / `tolerations` / `topologySpreadConstraints` / `priorityClassName` / `securityContext` (2.4+) | std K8s | — | **restart** |
| `sidecars[]` | []corev1.Container | custom sidecars | **restart** |
| `containers.pgbouncerConfig.resources` | Sidecar | resources for the operator's `pgbouncer-config` sidecar | restart |
| `minAvailable` | int-or-string | PDB | none |
| `metadata.labels/annotations` | map | pod metadata | rolls pods |

## 11. `backups` (pgBackRest)

Top: `backups.enabled` (bool, **2.7.0+**, default true — setting false removes backup infra; removing PVC-based repo contents additionally requires crunchy annotation `postgres-operator.crunchydata.com/authorizeBackupRemoval="true"`), `backups.trackLatestRestorableTime` (bool, **2.5.0+**, default true; polls repo to compute PITR watermark → extra S3 API calls), `backups.volumeSnapshots{className,mode,schedule}` (**2.9+ only**, feature gate `BackupSnapshots=true`).

### `backups.pgbackrest.*`

| Path | Type | Allowed / default | Since | Restart |
|---|---|---|---|---|
| `image` | string | e.g. `percona/percona-pgbackrest:2.55.0` (≥2.7; older: operator image); or `RELATED_IMAGE_PGBACKREST` | 2.0 | restarts repo-host & sidecars |
| `metadata.labels/annotations` | map | on pgBackRest pods | 2.0 | rolls |
| `configuration[]` | []VolumeProjection | custom config mounted at `/etc/pgbackrest/conf.d`. Cloud creds go here: Secret with keys `s3.conf` / `gcs.conf` (+`gcs-key.json`) / `azure.conf`, INI body `[global]\nrepoN-s3-key=…\nrepoN-s3-key-secret=…` (or `repoN-gcs-key=/etc/pgbackrest/conf.d/gcs-key.json`, `repoN-azure-account/key`). Multiple repos can share one Secret | 2.0 | config reload |
| `global` | map[string]string | any pgBackRest `[global]` option: `repoN-path`, `repoN-retention-full` + `repoN-retention-full-type: count\|time`, `repoN-cipher-type: aes-256-cbc`, `repoN-s3-uri-style: path`, `repoN-storage-verify-tls`, `process-max`, `archive-async`, `spool-path`, `log-level-console`, `repoN-bundle`, `repoN-block`… | 2.0 | reload |
| `repos[]` | 1–4 items, key `name` | `name` **required** `^repo[1-4]`; plus **exactly one of** `volume.volumeClaimSpec` (PVC repo → dedicated repo-host STS) / `s3{bucket,endpoint,region}` (all 3 required) / `gcs{bucket}` / `azure{container}`; `schedules{full,differential,incremental}` cron strings (minLength 6, standard cron) → CronJobs `<cluster>-repoN-{full,diff,incr}` | 2.0 | adding/changing repo triggers stanza-create; changing repo1 backing store is disruptive |
| `repoHost` | object | `affinity, tolerations[], topologySpreadConstraints[], priorityClassName, resources` (2.7+), `securityContext` (2.4+), `sshConfigMap/sshSecret` (deprecated) | 2.0 | repo-host restart |
| `manual` | object | `repoName` (required, `^repo[1-4]`), `options[]` (pgbackrest backup CLI, e.g. `--type=full|diff|incr`), `initialDelaySeconds` int64 (**2.7.0+**) — this section only *parameterizes* manual backups; the trigger is the annotation / pg-backup CR | 2.0 | none |
| `restore` | object | `enabled` (default false) + inline PostgresClusterDataSource (`repoName` required, `options[]`, `clusterName`, `clusterNamespace`, `resources`, `affinity`, `tolerations`, `priorityClassName`). **Managed by the pg-restore controller — don't drive it by hand; create a PerconaPGRestore instead** | 2.0 | restore = full cluster re-init |
| `jobs` | object | `resources`, `priorityClassName`, `affinity`, `tolerations[]`, `ttlSecondsAfterFinished` (min 60), `securityContext` (2.4+), `backoffLimit` int32 (**2.6.0+**, default 0), `restartPolicy` `OnFailure|Never` (**2.6.0+**) — applies to manual+scheduled+replica-create backup jobs | 2.0 | none |
| `containers.pgbackrest.resources` / `containers.pgbackrestConfig.resources` | Sidecar | resources of the two operator sidecars in instance pods (**2.4.0+**; the old alias `sidecars.*` is deprecated and only honored when crVersion < 2.4.0) | 2.4 | restart |
| `initContainer` | `{image,resources,containerSecurityContext}` | **2.7.0+** | 2.7 | restart |

Notes: repo1 is special — it's the WAL-archive + replica-create repo and the operator makes an automatic initial full backup there. `kubectl get pg-backup` lists all backups (the operator creates PerconaPGBackup objects for scheduled and replica-create backups too). Deleting a `pg-backup` object removes the physical backup (finalizer `internal.percona.com/delete-backup`). Backup retention = `global.repoN-retention-*`.

## 12. `pmm` (monitoring)

Required when `enabled: true`: `image`, `serverHost`, `secret`, `querySource`. Fields: `enabled` (bool, default false), `image` (`percona/pmm-client:2.x|3.x`), `imagePullPolicy`, `serverHost` (PMM server addr, default `monitoring-service`), `secret` (default `<cluster>-pmm-secret`; key **`PMM_SERVER_KEY`** = API key for PMM2, **`PMM_SERVER_TOKEN`** = service-account token for PMM3 — PMM3 requires operator ≥ **2.7.0**), `querySource` enum `pgstatmonitor` (default) | `pgstatstatements` (**2.5.0+**), `customClusterName` (**2.7.0+**), `postgresParams` (**2.7.0+**, extra args to `pmm-admin add postgresql`), `resources`, `containerSecurityContext`, `runtimeClassName`. Toggling PMM adds/removes a sidecar on every instance pod → **rolling restart**; rotating the PMM secret also rolls pods (hash annotations `pgv2.percona.com/pmm-secret-hash`, `.../monitor-user-secret-hash`). PMM auto-creates reserved PG user `monitor` (SUPERUSER).

## 13. `extensions`

- `extensions.builtin`: `pg_stat_monitor` (default **true** ≤2.8, **false** ≥2.9), `pg_audit` (default **true**), `pg_stat_statements` (default false, **2.7.0+**), `pgvector` (default false, **2.6.0+**, not for PG12), `pg_repack` (default false, **2.7.0+**). Toggling edits `shared_preload_libraries`(+ e.g. `pg_stat_statements.track=all`, `pgsm_query_max_len=2048`) → **automatic rolling PG restart**.
- Custom extensions: `extensions.image` (loader image; optional for builtins since 2.6), `extensions.imagePullPolicy`, `extensions.storage{type: s3|gcs|azure (CRD enum; docs: only s3 supported through 2.7), bucket, region, endpoint, secret{name}}` (secret keys `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`), `extensions.custom[]{name, version, checksum}`. Archive naming `<name>-pg<major>-<version>.tar.gz`; installed via init container; installed set reported in `.status.installedCustomExtensions`; removal from list uninstalls files. 2.8 adds `storage.forcePathStyle`, `storage.disableSSL`.

## 14. `secrets` / TLS

| Path | Notes |
|---|---|
| `secrets.customTLSSecret{name,items[]}` | server cert for external conns; keys `tls.crt`,`tls.key`,`ca.crt`; mounted `/pgconf/tls`. **If set, customReplicationTLSSecret MUST also be set, same ca.crt.** SANs must cover `<cluster>-ha.<ns>.svc` etc. Change → rolling restart |
| `secrets.customReplicationTLSSecret{name,items[]}` | client cert for replication; CN must be `_crunchyrepl` |
| `secrets.customRootCATLSSecret{name,items[]}` | **2.5.0+**; keys `root.crt`,`root.key` (use items[] to map tls.crt→root.crt etc.); own CA used to generate everything else |
| (2.9+) `spec.tls.*ValidityDuration` | cert-manager-managed cert lifetimes |

Default: operator self-generates CA + certs per cluster. pgBouncer has its own `proxy.pgBouncer.customTLSSecret`.

## 15. ACTION annotations & imperative ops (all on the `pg` CR unless noted)

| Annotation | Effect |
|---|---|
| `postgres-operator.crunchydata.com/trigger-switchover=<any changing value>` | executes switchover/failover per `spec.patroni.switchover` (must be `enabled: true`). Timestamp value recommended |
| `pgv2.percona.com/pgbackrest-backup=<id>` (equivalently crunchy-prefixed) | starts a manual backup using `backups.pgbackrest.manual` (repoName/options must be set). **Preferred path: create a PerconaPGBackup CR** — the operator then sets this annotation + `pgv2.percona.com/backup-in-progress=<pg-backup-name>` itself and syncs `manual.repoName/options` from the CR. Only one backup at a time (second CR errors "backup X already in progress") |
| `postgres-operator.crunchydata.com/pgbackrest-restore=<id>` | marks in-place restore; **set by the pg-restore controller** together with `spec.backups.pgbackrest.restore.{enabled:true,repoName,options}`. To **cancel a stuck restore**: `kubectl annotate pg <c> postgres-operator.crunchydata.com/pgbackrest-restore-` |
| `postgres-operator.crunchydata.com/authorizeBackupRemoval="true"` | permits deleting PVC-based repo backups when disabling backups (`backups.enabled: false`) |
| `postgres-operator.crunchydata.com/autoCreateUserSchema="true"` | crunchy-level equivalent of `spec.autoCreateUserSchema` (operator sets it automatically) |
| `postgres-operator.crunchydata.com/pgbackrest-ip-version=IPv6` | IPv6 wildcard for pgBackRest tls-server-address |
| `postgres-operator.crunchydata.com/postgres-exporter-collectors=None` | disable default exporter collectors (Pods) |
| `percona.com/override-config` (on operator-generated ConfigMaps) | 2.7+: operator stops reconciling that ConfigMap (manual Patroni config control) |
| `pgv2.percona.com/custom-patroni-version="3"\|"4"` (CR metadata) | 2.6–2.7: skip patroni-version-check Pod. Ignored ≥2.8 |
| Status/bookkeeping (read-only): `pgv2.percona.com/backup-in-progress`, `pgv2.percona.com/cluster-bootstrap-restore`, `pgv2.percona.com/patroni-version`, `pgv2.percona.com/pgbackrest-backup-job-name|-job-type` (on pg-backup objects), `pgv2.percona.com/pmm-secret-hash`, `pgv2.percona.com/monitor-user-secret-hash`, `postgres-operator.crunchydata.com/pgbackrest-hash`, `.../pgbackrest-backup-job-completion` | — |

### Companion CR specs (the clean kubectl trigger surface)
- **PerconaPGBackup**: `spec.pgCluster` (required), `spec.repoName` (required `^repo[1-4]`), `spec.options[]` (pgbackrest backup CLI, e.g. `--type=full`). Status: `state` `""→Starting→Running→Succeeded|Failed`, `jobName`, `backupType full|differential|incremental`, `storageType filesystem|s3|gcs|azure`, `destination`, `backupName` (pgbackrest set label, use for `--set`), `completed`, `latestRestorableTime` (format `YYYY-MM-DD HH:MM:SS.ffffff±ZZZZ`), `crVersion`, `repo`, `image`. Operator appends `--annotation="percona.com/backup-name"="<cr-name>"` to every backup.
- **PerconaPGRestore**: `spec.pgCluster`, `spec.repoName`, `spec.options[]` (`--type=default|immediate|time`, `--target="YYYY-MM-DD HH:MM:SS+TZ"`, `--set=<backupID>`). Status: `state` `""→Starting→Running→Succeeded|Failed`, `jobName`, `completed`. In-place restore stops the whole cluster and re-inits from the repo (destructive).
- **PerconaPGUpgrade** (major upgrade, cluster must be paused? no — must be `ready`; operator shuts it down during upgrade): required `postgresClusterName`, `image` (…`-upgrade` image), `fromPostgresVersion`, `toPostgresVersion`, `toPostgresImage`, `toPgBouncerImage`, `toPgBackRestImage`; optional `resources`, `tolerations`, `affinity`, `priorityClassName`, `metadata`, `imagePullPolicy`, `imagePullSecrets`, `initContainers`, `volumeMounts`. Available since 2.4.0 (GA hardening through 2.9).

## 16. Immutable / limited options (docs `immutable-options.md`)

- `metadata.name` — immutable. `dataSource` — creation-time only. `databaseInitSQL` — creation-time only.
- `users[].databases` — additive only (no revoke). `users[].options` — ignored for `postgres`.
- PVC storage — grow only (operator auto-expands ≥2.5.0); never shrink; storageClassName/accessModes immutable on existing PVCs.
- pgBackRest encryption settings (`repoN-cipher-type`) — cannot change after the stanza exists; new repo required.
- Operator-enforced PG params (cannot override): `ssl`, `ssl_cert_file`, `ssl_key_file`, `ssl_ca_file`, `unix_socket_directories`, `log_file_mode`, `archive_mode`, `archive_command`, plus extension-driven `shared_preload_libraries` entries; (2.8+) `track_commit_timestamp` when trackLatestRestorableTime.
- Patroni DCS keys other than `postgresql.parameters` / `postgresql.pg_hba` are ignored.

## 17. Version caveat matrix (operator/crVersion gates; verified by diffing tags + release notes)

| Feature | Min version |
|---|---|
| exposeReplicas; tablespaceVolumes; securityContext (instances/pgBouncer/jobs/repoHost); `containers.*` sidecar-resources naming (old `sidecars.*` deprecated); instances[].containers.replicaCertCopy; extensions.storage.endpoint; restore/data-move tolerations; PerconaPGUpgrade automation; S3 IAM-role auth; pg-backup `status.backupName`/latestRestorableTime | **2.4.0** |
| secrets.customRootCATLSSecret; backups.trackLatestRestorableTime; pmm.querySource; automatic PVC expansion + `AutoGrowVolumes` gate (limits.storage); Azure blob repos & AKS; auto pg-restore on clone; restricted (OpenShift-safe) container security contexts | **2.5.0** |
| PG17 (max 12–17); spec.metadata global labels/annotations honored; tlsOnly; autoCreateUserSchema (default true from here); extensions.builtin.pgvector; jobs.backoffLimit + jobs.restartPolicy; finalizer percona.com/delete-backups; Patroni 4 support (+patroni-version-check pod, custom-patroni-version annotation); custom restore_command override; extensions.image optional for builtins | **2.6.0** |
| backups.enabled (disable backups); users[].grantPublicSchemaAccess (PG≥15); initContainer (top/instance/pgbackrest); manual.initialDelaySeconds; patroni.createReplicaMethods; PMM3 (+pmm.customClusterName, pmm.postgresParams); builtin pg_repack + pg_stat_statements; repoHost.resources; percona.com/override-config; official percona-pgbouncer/percona-pgbackrest images; status.conditions | **2.7.0** |
| env/envFrom (instances, pgBouncer, pgbackrest); expose*.loadBalancerClass; custom password+verifier fully respected; huge pages support; extensions.storage.{forcePathStyle,disableSSL}; custom-patroni-version annotation ignored; track_commit_timestamp enforcement | 2.8.0 |
| PVC snapshots (backups.volumeSnapshots + dataSource VolumeSnapshot); standby.maxAcceptableLag; authentication.rules (LDAP) + config.files; spec.tls validity durations (cert-manager); clusterServiceDNSSuffix; sidecarVolumes/sidecarPVCs; wal_level guidance; PG18 default/PG13 EOL; pg_stat_monitor default→false; PMM2 deprecated | 2.9.0 |
| Upstream CRDs renamed `postgres-operator.crunchydata.com` → `upstream.pgv2.percona.com` (breaking for anything reading the crunchy CR) | 3.0.0 |

## 18. Console implementation notes (kubectl-only)

- Use `kubectl get pg/pg-backup/pg-restore -o json`; wait on `.status.state`. `kubectl get pg <c> -o jsonpath='{.status.state}'` → `initializing|paused|stopping|ready|error`.
- Primary discovery: `kubectl get pods -l postgres-operator.crunchydata.com/cluster=<c>,postgres-operator.crunchydata.com/role=master`.
- Config edits: `kubectl patch pg <c> --type=merge -p '<json>'` is the documented pattern (server-side apply also fine). Never touch the crunchy `postgresclusters` object.
- To watch restore progress in-place you may also need the crunchy CR's `.status.pgbackrest.restore` — but treat as read-only (and remember the group rename at 3.0).
- Fields whose Go doc-comments explicitly say "Changing this value causes PostgreSQL/PgBouncer/repo-host to restart" are listed above; everything under `expose*`, `config.global` (pgBouncer), `patroni.dynamicConfiguration` (non-postmaster), schedules, manual/restore params, users, metadata-only changes are non-disruptive.
- pgbench-relevant knobs: `patroni.dynamicConfiguration.postgresql.parameters` (shared_buffers, work_mem, max_connections, wal_level, max_worker_processes…, auto rolling-restart when needed), `proxy.pgBouncer.config.global.pool_mode`, `instances[].resources`, `backups.pgbackrest.global.repoN-retention-*`.

<!-- MACHINE_CATALOG -->
```json
{"crd":{"group":"pgv2.percona.com","version":"v2","kind":"PerconaPGCluster","plural":"perconapgclusters","shortName":"pg","requiredSpec":["backups","instances","postgresVersion"],"companions":[{"kind":"PerconaPGBackup","shortName":"pg-backup","spec":{"pgCluster":"string,required","repoName":"string,required,^repo[1-4]","options":"[]string pgbackrest backup CLI"},"statusStates":["","Starting","Running","Succeeded","Failed"]},{"kind":"PerconaPGRestore","shortName":"pg-restore","spec":{"pgCluster":"string,required","repoName":"string,required,^repo[1-4]","options":"[]string pgbackrest restore CLI (--type, --target, --set)"},"statusStates":["","Starting","Running","Succeeded","Failed"]},{"kind":"PerconaPGUpgrade","spec":{"postgresClusterName":"required","image":"required","fromPostgresVersion":"required int","toPostgresVersion":"required int","toPostgresImage":"required","toPgBouncerImage":"required","toPgBackRestImage":"required"}}]},"fields":[{"path":"spec.crVersion","type":"string","default":"operator version","restart":"indirect","since":"2.0"},{"path":"spec.image","type":"string","restart":"pg-rolling","since":"2.0"},{"path":"spec.imagePullPolicy","type":"enum","allowed":["Always","Never","IfNotPresent"],"since":"2.0"},{"path":"spec.imagePullSecrets","type":"[]LocalObjectReference","restart":"all-pods","since":"2.0"},{"path":"spec.postgresVersion","type":"int","required":true,"min":12,"max":{"2.3-2.5":16,"2.6-2.7":17},"restart":"major-upgrade-only","since":"2.0"},{"path":"spec.port","type":"int32","default":5432,"min":1024,"restart":"pg","since":"2.0"},{"path":"spec.pause","type":"bool","default":false,"effect":"graceful shutdown; refused while backup job running","since":"2.0"},{"path":"spec.unmanaged","type":"bool","default":false,"effect":"stop reconciliation","since":"2.0"},{"path":"spec.openshift","type":"bool","default":"autodetect","since":"2.0"},{"path":"spec.tlsOnly","type":"bool","default":false,"restart":"pg","since":"2.6.0"},{"path":"spec.autoCreateUserSchema","type":"bool","default":"true when crVersion>=2.6.0","since":"2.6.0"},{"path":"spec.metadata.labels|annotations","type":"map","effect":"global labels/annotations on all created objects","since":"2.6.0"},{"path":"spec.initContainer","type":"{image,resources,containerSecurityContext}","since":"2.7.0","restart":"pg"},{"path":"spec.expose","type":"ServiceExpose{type(ClusterIP|NodePort|LoadBalancer,default ClusterIP),nodePort,annotations,labels,loadBalancerSourceRanges}","restart":"none","since":"2.0"},{"path":"spec.exposeReplicas","type":"ServiceExpose","restart":"none","since":"2.4.0"},{"path":"spec.standby","type":"{enabled(bool,false),repoName(^repo[1-4]),host,port(int32,min1024)}","promote":"set enabled=false","since":"2.0"},{"path":"spec.users[]","type":"[]{name(req,^[a-z0-9]([-a-z0-9]*[a-z0-9])?$,max63),databases[](additive-only),options(<=200,no ';'),password.type(ASCII|AlphaNumeric,default ASCII),secretName,grantPublicSchemaAccess(2.7.0+,PG>=15)}","secret":"<cluster>-pguser-<user> keys: user,password,verifier,host,port,dbname,uri,jdbc-uri,pgbouncer-*","defaultWhenEmpty":"user <cluster> with db <cluster>","reserved":"monitor","since":"2.0"},{"path":"spec.databaseInitSQL","type":"{name(req),key(req)}","immutable":"creation-only","since":"2.0"},{"path":"spec.dataSource","immutable":"creation-only","oneOf":{"postgresCluster":"{clusterName,clusterNamespace,repoName(req ^repo[1-4]),options[],resources,affinity,priorityClassName,tolerations[]}","pgbackrest":"{stanza(req),repo{name,s3{bucket,endpoint,region}|gcs{bucket}|azure{container}|volume}(req),configuration[],global{},options[],resources,affinity,priorityClassName,tolerations[]}","volumes":"{pgDataVolume{pvcName(req),directory,tolerations,annotations,labels},pgWALVolume{...},pgBackRestVolume{...}}"},"since":"2.0"},{"path":"spec.instances[]","minItems":1,"listMapKey":"name","required":["dataVolumeClaimSpec"],"fields":{"name":"pattern ^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$ default '' -> NN; len(cluster)+len(name)<=46","replicas":"int32 default 1 min 1","metadata":"labels/annotations","resources":"ResourceRequirements (restart)","dataVolumeClaimSpec":"PVCSpec required; requests.storage grow auto>=2.5.0; limits.storage=AutoGrowVolumes cap","walVolumeClaimSpec":"PVCSpec optional (restart+move)","tablespaceVolumes":"[]{name ^[a-z][a-z0-9]*$,dataVolumeClaimSpec} since 2.4.0 feature-gate TablespaceVolumes","affinity":"restart","tolerations":"restart","topologySpreadConstraints":"restart","priorityClassName":"restart","securityContext":"PodSecurityContext since 2.4.0 (restart)","sidecars":"[]corev1.Container (restart)","containers.replicaCertCopy.resources":"since 2.4.0","initContainers":"[]corev1.Container (restart)","initContainer":"{image,resources,containerSecurityContext} since 2.7.0","minAvailable":"int-or-string PDB","volumeMounts":"[]VolumeMount (restart)"}},{"path":"spec.patroni","fields":{"syncPeriodSeconds":"int32 default 10 min 1 (pg restart)","leaderLeaseDurationSeconds":"int32 default 30 min 3 (pg restart)","port":"int32 default 8008 min 1024 (pg restart)","dynamicConfiguration":"schemaless; only postgresql.parameters + postgresql.pg_hba honored; no validation; postmaster params -> auto rolling restart via patronictl restart --pending","switchover":"{enabled bool default false, targetInstance string (instance name; required for Failover), type Switchover|Failover default Switchover}","createReplicaMethods":"[]enum basebackup|pgbackrest since 2.7.0"}},{"path":"spec.proxy.pgBouncer","note":"deployed by default; disable via replicas=0","fields":{"replicas":"int32 default 1 min 0","image":"string (restart)","port":"int32 default 5432 min 1024 (restart)","exposeSuperusers":"bool","config.global":"map ini settings hot-reload no validation","config.databases":"map","config.users":"map","config.files":"[]VolumeProjection /etc/pgbouncer","customTLSSecret":"SecretProjection (restart)","expose":"ServiceExpose","resources":"restart","affinity|tolerations|topologySpreadConstraints|priorityClassName":"restart","securityContext":"since 2.4.0","sidecars":"[]corev1.Container (restart)","containers.pgbouncerConfig.resources":"sidecar","minAvailable":"int-or-string","metadata":"labels/annotations"}},{"path":"spec.backups","fields":{"enabled":"bool default true since 2.7.0; disabling PVC repos needs crunchy annotation authorizeBackupRemoval","trackLatestRestorableTime":"bool default true since 2.5.0","pgbackrest.image":"string","pgbackrest.metadata":"labels/annotations","pgbackrest.configuration":"[]VolumeProjection; secret keys s3.conf/gcs.conf+gcs-key.json/azure.conf with [global] repoN-* creds","pgbackrest.global":"map any pgbackrest global option (retention, cipher, path, uri-style,...)","pgbackrest.repos":"1-4 items name ^repo[1-4]; oneOf volume.volumeClaimSpec|s3{bucket,endpoint,region}|gcs{bucket}|azure{container}; schedules{full,differential,incremental} cron minLength 6","pgbackrest.repoHost":"{affinity,tolerations,topologySpreadConstraints,priorityClassName,resources(2.7+),securityContext(2.4+)}","pgbackrest.manual":"{repoName req,options[],initialDelaySeconds(2.7.0+)}","pgbackrest.restore":"{enabled,repoName req,options,clusterName,clusterNamespace,resources,affinity,tolerations,priorityClassName} - managed by pg-restore controller","pgbackrest.jobs":"{resources,priorityClassName,affinity,tolerations,ttlSecondsAfterFinished(min60),securityContext(2.4+),backoffLimit(2.6+),restartPolicy(2.6+)}","pgbackrest.containers":"{pgbackrest.resources,pgbackrestConfig.resources} since 2.4.0 ('sidecars' alias deprecated)","pgbackrest.initContainer":"since 2.7.0"}},{"path":"spec.pmm","requiredWhenEnabled":["image","serverHost","secret","querySource"],"fields":{"enabled":"bool default false (toggles sidecar -> rolling restart)","image":"pmm-client image","imagePullPolicy":"enum","serverHost":"default monitoring-service","secret":"default <cluster>-pmm-secret; key PMM_SERVER_KEY (PMM2) or PMM_SERVER_TOKEN (PMM3, 2.7.0+)","querySource":"pgstatmonitor|pgstatstatements default pgstatmonitor since 2.5.0","customClusterName":"since 2.7.0","postgresParams":"since 2.7.0","resources":"","containerSecurityContext":"","runtimeClassName":""}},{"path":"spec.extensions","fields":{"image":"loader image (optional for builtins since 2.6)","imagePullPolicy":"","storage":"{type enum s3|gcs|azure (docs: s3 only <=2.7),bucket,region,endpoint,secret{name} keys AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY}","builtin":"{pg_stat_monitor default true(<=2.8)/false(2.9+), pg_audit default true, pg_stat_statements default false 2.7+, pgvector default false 2.6+ (no PG12), pg_repack default false 2.7+} - toggling => rolling pg restart","custom":"[]{name,version,checksum}"}},{"path":"spec.secrets","fields":{"customTLSSecret":"{name,items[]} tls.crt/tls.key/ca.crt; requires customReplicationTLSSecret with same ca.crt; restart","customReplicationTLSSecret":"{name,items[]} CN must be _crunchyrepl","customRootCATLSSecret":"{name,items[]} root.crt/root.key since 2.5.0"}}],"actions":{"switchover":{"preset":"spec.patroni.switchover{enabled:true,targetInstance?,type:Switchover|Failover}","trigger":"annotate pg <c> postgres-operator.crunchydata.com/trigger-switchover=<changing value>","impl":"patronictl switchover --scheduled=now --force [--candidate]; failover: patronictl failover --force","cleanup":"set enabled=false"},"manualBackup":{"preferred":"create PerconaPGBackup{pgCluster,repoName,options}","annotationPath":"pgv2.percona.com/pgbackrest-backup=<id> (maps to crunchy prefix) + backups.pgbackrest.manual preset","concurrencyGuard":"pgv2.percona.com/backup-in-progress"},"inPlaceRestore":{"preferred":"create PerconaPGRestore{pgCluster,repoName,options: --type=default|immediate|time,--target,--set}","cancel":"kubectl annotate pg <c> postgres-operator.crunchydata.com/pgbackrest-restore-"},"pause":"spec.pause=true (refused during backup)","stopReconcile":"spec.unmanaged=true","promoteStandby":"spec.standby.enabled=false","majorUpgrade":"create PerconaPGUpgrade","disableBackupsPVCRemoval":"annotate postgres-operator.crunchydata.com/authorizeBackupRemoval=true","overridePatroniConfigMap":"annotate ConfigMap percona.com/override-config (2.7+)","deleteBackup":"delete pg-backup object (finalizer internal.percona.com/delete-backup removes repo backup)"},"finalizers":["percona.com/delete-pvc","percona.com/delete-ssl","percona.com/delete-backups(2.6+,tech-preview)"],"statusStates":["initializing","paused","stopping","ready","error"],"roleLabels":{"cluster":"postgres-operator.crunchydata.com/cluster","instance":"postgres-operator.crunchydata.com/instance","instanceSet":"postgres-operator.crunchydata.com/instance-set","role":"postgres-operator.crunchydata.com/role=master|replica"},"versionGates":{"2.4.0":["exposeReplicas","tablespaceVolumes","securityContext blocks","containers.* sidecar resources","extensions.storage.endpoint","PerconaPGUpgrade","restore/data-move tolerations"],"2.5.0":["customRootCATLSSecret","trackLatestRestorableTime","pmm.querySource","auto PVC expansion","AutoGrowVolumes gate","Azure repos"],"2.6.0":["PG17","spec.metadata honored","tlsOnly","autoCreateUserSchema","pgvector","jobs.backoffLimit","jobs.restartPolicy","delete-backups finalizer","Patroni4 + custom-patroni-version annotation","custom restore_command"],"2.7.0":["backups.enabled","grantPublicSchemaAccess","initContainer blocks","manual.initialDelaySeconds","createReplicaMethods","PMM3 + customClusterName + postgresParams","pg_repack","pg_stat_statements","repoHost.resources","override-config annotation","status.conditions"],"2.8.0":["env/envFrom","loadBalancerClass","custom password+verifier","extensions forcePathStyle/disableSSL","custom-patroni-version ignored"],"2.9.0":["volumeSnapshots","dataSource VolumeSnapshot","maxAcceptableLag","authentication.rules","config.files","tls validity durations","clusterServiceDNSSuffix","sidecarVolumes/sidecarPVCs","PG18","pg_stat_monitor default false"],"3.0.0":["crunchy CRDs renamed to upstream.pgv2.percona.com"]}}
```
