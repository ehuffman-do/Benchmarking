# Completeness critique — 7-report research program for kubectl-free PG-on-K8s console

## 1. Topics missed entirely

1. **RBAC verb matrix for the console's ServiceAccount.** Zero coverage. Needed per operation: `get/list/watch/patch` on `perconapgclusters`, `postgresclusters`; `create/delete` on `perconapgbackups`/`perconapgrestores`/`pgupgrades`; `create` on **`pods/exec`** (every patronictl/psql read path in day2-ops and ux-intel silently requires this — a major security decision nobody surfaced); `get` on `pods/log`; `get` on Secrets (`<cluster>-pguser-*` connection creds, pgBackRest secrets); `list/watch` Events, Jobs, CronJobs, PVCs, Services, StatefulSets, Endpoints (Patroni DCS). Also: Role vs ClusterRole for namespace-scoped tenancy, and whether the console should use **user impersonation** so audit trails attribute actions to humans, not the SA.

2. **The console is kubectl-FREE, but all seven reports assume kubectl CLI idioms.** No research on the API-machinery equivalents: exec subresource over SPDY/WebSocket, informers/watch bookmarks instead of `kubectl get -w`, **server-side apply** (fieldManager, conflict semantics — SSA *does* do keyed merges on `listType=map` arrays like `spec.instances`, making crunchy-cr's "use `--type json` positional ops" advice actively worse than SSA), and `dryRun=All` for the preflight step ux-intel's runbook pattern (A3) demands.

3. **CRD schema introspection at runtime.** Nothing on `kubectl explain --recursive` equivalents (OpenAPI v3 discovery, `/openapi/v3/apis/pgv2.percona.com/v2`) to drive dynamic forms and validate fields against the *installed* CRD version instead of the baked-in v2.7.0/v5.8.8 catalogs. This is the only durable answer to the version-skew problem that infects every report.

4. **Operator version detection & upgrade paths.** How does the console learn the operator version (Deployment image tag? CRD annotations? `status.conditions`)? What happens across Percona operator upgrades (crVersion skew rules are described but not the upgrade *procedure*), Crunchy v5→v6 (repo main is already v6.0.2 — presumably new served API version), and the flagged-but-unexplored **Percona 3.0 group rename to `upstream.pgv2.percona.com`**, which breaks the "one codepath, ~80% shared" architecture premise. OLM vs Helm vs manifest install variants also unaddressed.

5. **Multi-cluster / fleet management.** ux-intel designs a fleet home screen, but no report covers kubeconfig/context handling, per-K8s-cluster credentials and RBAC divergence, caching/latency across API servers, or cross-K8s-cluster standby (DR) topologies beyond one topology-node mention.

6. **GitOps ownership conflict.** If Argo CD/Flux/Helm owns the CR, every console patch gets reverted or triggers drift alarms. Console needs owner detection (managedFields, `argocd.argoproj.io/*` labels, `helm.sh/release` annotations) and a "this cluster is GitOps-managed — export patch instead of applying" mode. Completely absent.

7. **PG 17/18 monitoring-surface changes.** pg-gucs proved `pg_settings` columns identical 13–17 but the console's *query catalog* wasn't researched: PG17 moved checkpointer stats out of `pg_stat_bgwriter` into `pg_stat_checkpointer`, PG16 added `pg_stat_io`, PG17 added `sync_replication_slots`/failover slot sync and `allow_alter_system` (interacts with Patroni 4). And **PG 18 is missing entirely** despite crunchy-cr stating v5.8 supports 11–18 (confirmed: `ssl_groups` CEL rule gated `postgresVersion > 17` in `pgo/pkg/apis/.../postgrescluster_types.go:20`); PG 11/12 legacy clusters also unexamined.

8. **Metrics acquisition path.** ux-intel prescribes sparklines/lag charts but no report covers where numbers come from: exporter port 9187 scrape vs Prometheus API vs direct SQL, pgMonitor query packs, PMM integration on the Percona side, monitoring-user credentials (`ccp_monitoring`).

9. **Events/audit persistence.** K8s Events have ~1h etcd TTL by default — the activity feed and audit tab (ux-intel A1) cannot be built on `kubectl get events` alone; needs a persistence design. Postgres/Patroni/pgBackRest log acquisition (logging_collector, log rotation, Job log retention after TTL-controller cleanup) also unresearched.

10. **Failure-mode runbooks.** day2-ops catalogs happy paths and status conditions but not recovery: stuck restore (`pgbackrest-restore` Job failure), stuck finalizers on CR delete, PGUpgrade preconditions/failure rollback, WAL-disk-full, PVC resize `Infeasible`/`PersistentVolumeResizeError` handling, split-brain / Patroni DCS loss.

11. **Connection-info surface.** User secret key layout (`user`, `password`, `uri`, `jdbc-uri`, `host`, `port`), external exposure (LoadBalancer/NodePort service types in spec), pgBouncer TLS modes, custom TLS/cert-manager rotation — needed for any "Connect" panel; scattered or absent.

12. **Extensions & pg_hba workflow.** Percona `spec.extensions` (custom/built-in) vs Crunchy's lack thereof; per-DB `CREATE EXTENSION` orchestration; `spec.authentication.rules` / Patroni `pg_hba` editing as a console feature — GUC visibility caveat is covered but not the management workflow.

13. **PodDisruptionBudgets, priorityClass, operator HA, watchNamespace scoping** — thin or absent; matters for the ops console's "why won't this pod evict" answers.

## 2. Claims that look wrong or version-fragile — verify against live cluster, don't bake in

1. **Percona `status.state` includes `error` (percona-cr) — contradicted by day2-ops (4 states) and by source: `ppg/pkg/apis/pgv2.percona.com/v2/perconapgcluster_types.go:527-533` defines only `initializing|paused|stopping|ready`.** I confirmed no `error` AppState in the current main clone. One of the two reports is wrong; resolve and treat the enum as open-ended in code regardless.
2. **`kubectl get pg` shortname.** `pg` collides with Zalando postgres-operator's `postgresqls.acid.zalan.do` shortname. Console must always use fully-qualified `plural.group` — reports repeatedly showcase shortnames.
3. **"Patroni-forced params show `source='command line'`" (pg-gucs).** Patroni-version- and parameter-dependent; verify per cluster from a live `pg_settings` snapshot rather than encoding the heuristic.
4. **`backup-standby: prefer` needs pgBackRest ≥2.54** — correct today, but pgBackRest version is an *image* property; detect via `pgbackrest version` exec (or image tag), never assume from operator version.
5. **"Scheduled backups can't carry extra options... ~line 3110"** — line-number citations rot; also the very next operator minor could add schedule options. Re-derive from installed CRD schema.
6. **pgBouncer catalog from master (1.24/1.25-era)** — the deployed pgbouncer image version differs per operator release; verify with `SHOW VERSION` and gate option availability dynamically.
7. **PG version ranges ("v5.8: 11–18; v5.7: 10–17")** — derive from operator Deployment's `RELATED_IMAGE_POSTGRES_*` env at runtime, which is the actual truth for what images resolve.
8. **Annotation prefix-rewrite `pgv2.percona.com/ → postgres-operator.crunchydata.com/`** — verified at v2.7.0 only; behavior and the copied set can change per release (and definitely at 3.0). Probe: write with the crunchy prefix (passed as-is per the same report) as the safer invariant, but confirm per installed version.
9. **`metadata.name` ≤22 chars "recommended" / 46-char combined limit** — soft heuristic; enforce via server dry-run + CRD/CEL validation errors, not a hardcoded limit.
10. **"docs git source identical to 403-blocked docs site"** — unverifiable equivalence; docs-source-derived claims (immutable options, procedures) should be re-validated behaviorally (attempt + observe webhook/CEL rejection or operator ignore).
11. **Percona required fields: `backups` required at CRD level vs repos `+optional` with CEL "≥1 repo when backups enabled"** — internally tense (2.5+ added backup-less operation); reconcile against the installed CRD, per-version.
12. **pgBackRest option catalog built from `main` (~2.59-dev)** — pre-release; shipped images lag by several releases. Same remedy as #4.

## 3. Five highest-risk assumptions

1. **"One codepath covers both operators (~80% shared)."** Rests on Percona keeping Crunchy labels/annotations and the internal `postgres-operator.crunchydata.com` group — already scheduled to break (3.0 `upstream.pgv2.percona.com` rename), and backup/restore flows already diverge (CRs vs annotations). Mitigation: runtime capability discovery (CRDs present, group names, annotation probes), not compile-time branching.
2. **Version-pinned option catalogs (CRD v2.7.0/v5.8.8, pgBackRest main, pgBouncer master, GUCs 13–17) baked into the console.** Real fleets run mixed versions in both directions. The catalogs should be *doc/UX overlays* keyed to values obtained by live introspection (OpenAPI v3, `pg_settings`, `pgbackrest version`, `SHOW VERSION`), never the source of truth for what's settable.
3. **kubectl semantics translate cleanly to a kubectl-free implementation.** Exec-based reads (`patronictl list`, `psql`) require `pods/exec` RBAC and a healthy target pod; merge-patch vs SSA array semantics differ from what the reports recommend; annotate-trigger race behavior and `dryRun` preflight were never tested through the raw API. The entire write-path mechanics need a re-verification pass at the API layer.
4. **The console is the only writer.** No handling for GitOps controllers, human kubectl users, or the operator itself fighting console patches (SSA fieldManager conflicts, resourceVersion races, reverted edits). Without conflict/ownership design, "guided runbooks" will silently lose fights with Argo.
5. **Transient/eventually-consistent sources suffice for state and history:** K8s Events (1h TTL) for the activity feed/audit, `observedGeneration==generation && ready==size` as universal completion idiom (races when another actor bumps generation mid-operation; Percona's `state` enum is version-unstable per finding 2.1), and 10s patronictl polls for topology (blind during the exact incidents the console exists for — pod down, API slow). Each needs a fallback (Patroni REST 8008 direct, persisted event store, per-operation condition tracking).
