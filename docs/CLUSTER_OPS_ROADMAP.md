# Cluster Ops roadmap — from ops module to full PG-on-Kubernetes console

Synthesized from an eight-report research program (see `docs/research/`),
each report verified against primary sources: the Percona operator v2.7.0
CRD/Go types, Crunchy PGO v5.8.8, Patroni source, pgBackRest source
(config.yaml / backup.c), PostgreSQL 13–17 doc/source, and the option
surfaces of pgBouncer. The goal: anyone can operate PostgreSQL on
Kubernetes from this console — no kubectl, no SQL, no YAML — with every
action preflight-checked, dry-runnable, verified after apply, and visible
live.

## Where we are (shipped)

| Capability | Status |
|---|---|
| Kube Targets: register/edit/validate (verdict badge), discover topology | ✅ |
| CR config: dry-run diff → apply → verify loop, snapshots, rollback | ✅ |
| Backups: direct + operator paths, replica via `--backup-standby`, lock preflight, samplers, benchmark overlay | ✅ |
| Failover scenarios A/B/C1/C2 with stitcher + comparison | ✅ |
| Telemetry monitor (WAL/checkpoints/lag/disk, leader re-detection) | ✅ |
| **Parameter map**: live pg_settings introspection + apply-channel overlay, typed editors, staged apply | ✅ |
| **Sidecar catalogs**: pgBackRest (119 opts), Patroni DCS (31), pgBouncer (68) with per-operator CR paths; pgBackRest global click-to-apply | ✅ |
| **Diagnostics workbench**: 17 read-only checks, watch mode, live charts | ✅ |
| **Health engine**: 14 heuristics → findings with severity/remediation/deep-link | ✅ |

## Design principles (locked in by the research critique)

1. **Live introspection over baked catalogs.** `pg_settings`, the installed
   CRD schema, `pgbackrest version`, and pgBouncer `SHOW VERSION` are the
   source of truth for what is settable; static catalogs are doc/UX overlays
   only. Real fleets run mixed versions in both directions.
2. **Treat operator status enums as open-ended.** Percona's `status.state`
   set differs across reports/releases; never `else: impossible`.
3. **Fully-qualified resource names** in kubectl calls where ambiguity is
   possible (`pg` collides with Zalando's shortname; we use the full
   singular names already).
4. **Detect versions at runtime**: operator version from its Deployment
   image, pgBackRest via exec, PG via `server_version_num` — never assume
   from another component's version. Percona 3.0 renames the internal group
   (`upstream.pgv2.percona.com`), so operator capability discovery must be
   probed, not hardcoded.
5. **Every mutation follows one shape**: preflight → plan (dry-run diff) →
   typed confirmation → execute with live watch → verify → summary +
   rollback pointer. This is already the cr-apply/backup/scenario shape;
   every new operation adopts it.
6. **Ownership awareness**: before patching, detect GitOps managers
   (`argocd.argoproj.io/*`, `helm.sh/release`, managedFields) and warn /
   offer "export patch" instead of a silent fight with Argo.

## Phase 6 — the operations catalog (Tier A/B day-2 ops)

Research ranked 24 operations by real-world frequency (`docs/research/
day2-operations.md` includes a machine-readable catalog with mechanics,
completion conditions, and live signals per op). Health inspection, logs,
and backups — ranks 1–3 — are shipped. Next, in rank order:

1. **Restart** (rank 4): cluster-wide via `spec.metadata.annotations.
   restarted=<ts>` (rolling, replicas first), single member via
   `patronictl restart <scope> <member> --force`. Pairs with the
   `pending_restart` health finding — the finding's action becomes a
   one-click guided restart. Never `kubectl rollout restart` (fights the
   operator).
2. **Switchover / failover as an operation** (rank 6): CR-declarative
   (`spec.patroni.switchover{enabled,targetInstance,type}` + trigger
   annotation) with target picker. The scenario runner stays the
   *measurement* tool; this is the lightweight everyday control. Verify:
   role labels flipped, TL+1, `switchover` status field matches.
3. **User & database management** (rank 7): `spec.users[]` editor
   (name/databases/options/password type), password rotation (blank the
   secret's `password` key → operator regenerates), connection-info panel
   from the pguser secret keys (`host`, `port`, `uri`, `jdbc-uri`,
   `pgbouncer-*`) — never displaying the secret value itself, same
   redaction rules as everything else.
4. **Scale replicas** (rank 8): `spec.instances[i].replicas` with a
   watch on the new member's basebackup Job + streaming lag reaching 0;
   scale-down warns that member PVCs are deleted.
5. **Backup schedules & retention editor** (rank 10): cron editors per
   repo (`repos[i].schedules.{full,differential,incremental}`), retention
   via `global.repoN-retention-*` (already applyable through the
   pgBackRest tab), pause = remove schedule keys (never CronJob suspend —
   reverted by the operator; our schedule pause already does this
   correctly).
6. **Vertical resize** (rank 9): `spec.instances[i].resources` with the
   OOM-vs-shared_buffers preflight (compare request to shared_buffers from
   the parameter map) and rolling-recreate watch.

## Phase 7 — storage, upgrades, pooler, connectivity

- **PVC expansion** (rank 12; urgent when WAL fills — links from the disk
  health finding): preflight `allowVolumeExpansion` on the StorageClass,
  patch `dataVolumeClaimSpec.resources.requests.storage`, watch the
  `PersistentVolumeResizing/FileSystemResizePending` conditions, verify
  df. Percona AutoGrowVolumes feature gate surfaced as read-only info.
- **Minor version upgrade** (rank 11): single merge patch of
  `spec.image` + pgBouncer + pgBackRest images (Percona), watch the
  rolling order, verify `SELECT version()` on every member.
- **Pooler operations** (rank 13): pgBouncer scale (0 = pause),
  click-to-apply for the pgBouncer tab (new cr-apply action
  `pgbouncer_global` patching `proxy.pgBouncer.config.global`, verified
  via rendered ini / `SHOW CONFIG`), expose settings.
- **Node maintenance** (rank 14) as a guided runbook: map roles→nodes
  (already in topology), switchover off the node, cordon, drain
  (PDB-aware messaging), uncordon, verify streaming.

## Phase 8 — restore, clone, DR (the destructive tier)

Guarded wizards with double confirmation and cluster-name typing:

- **Clone to a new cluster** (rank 15, low risk, build first):
  `dataSource.postgresCluster{clusterName,repoName,options[--type=time
  --target ...]}` — also the safe way to test PITR targets.
- **In-place restore & PITR** (rank 16): Percona `PerconaPGRestore` CR /
  Crunchy restore + annotation; preflight shows `latestRestorableTime`
  and the backup set list (from `backup_info` diag); post-verify timeline
  bump + state ready. Abort affordance via the restore annotation removal.
- **Standby cluster & promotion** (rank 23) for DR drills.
- **Major version upgrade** (rank 22): `PerconaPGUpgrade` wizard with the
  full post-checklist (extensions, ANALYZE, fresh full backup).

## Phase 9 — enterprise hardening

- **Least-privilege ServiceAccount recipe** (replaces the cluster-admin
  quickstart): documented Role with exactly the verbs the console uses —
  get/list/watch on pods/CRs/jobs/events/pvcs/secrets(named), create on
  `pods/exec`, patch on the two CR kinds, create/delete on
  backup/restore CRs. Ships in OPERATIONS.md with a manifest.
- **SSO**: OIDC in front of the existing session auth; role mapping to
  viewer/operator/admin. Audit export (the audit table already records
  who/what/when) as CSV/JSONL for SOC 2 evidence.
- **Fleet view**: the targets list grows worst-health/last-backup/version
  columns — multi-cluster from day one since targets are already
  per-kubeconfig.
- **Persisted activity feed**: K8s Events TTL is ~1h; our ops runs +
  audit rows already persist — surface them as the cluster timeline and
  fold in operator events captured during runs.
- **API/webhook extensibility**: outbound webhook on run completion +
  health-status transitions (Slack/Teams-friendly payload).

## Intelligence: from point-in-time to continuous

Shipped health checks are point-in-time. Next:

1. **Scheduled health** — run `ops health` from the monitor lane on an
   interval; store history; badge transitions (ok→warn) instead of states.
2. **Trend rules** — disk-fill linear fit ("PVC full in ~6 days"), WAL
   generation vs archive drain, connection growth. The monitor CSVs
   already hold the series; rules read the last N samples.
3. **Deeper sources** — `pg_stat_io` (PG16+), `pg_stat_statements` top
   queries (extension-gated), pgBouncer `SHOW POOLS` wait counts,
   `pg_stat_progress_*` views during long operations.
4. **Runbook-linked findings** — every finding's action deep-links to the
   operation that fixes it (pending_restart → guided restart; disk → PVC
   expansion wizard; stale backup → backup form pre-filled).

## Explicit non-goals (for now)

- Rewriting the kubectl subprocess layer onto the raw K8s API (informers,
  SSA). The worker-owned kubectl model is a deliberate, field-proven
  choice; revisit only if exec-RBAC or scale demands it.
- Editing arbitrary CR YAML in the console. Every mutation stays a typed,
  named operation with its own preflight/verify — that's the safety model.
- Supporting operators other than Percona v2 / Crunchy v5 (Zalando,
  CloudNativePG) — the CR abstraction could stretch, but each adds a
  matrix of verification paths.
