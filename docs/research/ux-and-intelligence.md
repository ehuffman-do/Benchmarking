# Cluster Ops research: enterprise console UX (Part A) + built-in intelligence catalog (Part B)

Scope assumptions verified against primary sources: Percona Operator for PostgreSQL v2 (`perconapgcluster`, API group `pgv2.percona.com/v2`) is a fork of Crunchy PGO v5 (`postgrescluster`, `postgres-operator.crunchydata.com/v1beta1`) and **keeps Crunchy's pod labels and trigger annotations** (Percona's own docs tell you to run `kubectl annotate pg cluster1 postgres-operator.crunchydata.com/trigger-switchover="$(date)"`). This means one kubectl-driven code path covers both operators for most reads, with operator-specific branches only for backup/restore CRs and spec paths.

---

# PART A — UX patterns from best-in-class infra consoles

## A1. Information architecture: fleet → cluster → component → object

Consensus pattern across Rancher, Aiven, Datadog, PMM, Supabase:

- **Level 0 — Fleet home** (Rancher cluster list, Aiven services list): one table row per cluster. Columns: status chip, operator type/version badge (Percona v2 / PGO v5), PG major version, primary instance name, replicas ready `n/m`, pgBouncer ready, last-successful-backup age, open-alert count by severity, and a 1-hour sparkline (TPS or connections). Row click → cluster detail. Global namespace/kubecontext filter persists in URL.
- **Level 1 — Cluster overview**: hero strip of 5–8 stat tiles (health score, connections %, replication lag, disk %, backup age, active alerts), a live topology graph (A2), and the most recent activity feed entries. This page must answer "is anything wrong and what changed recently" in <5 seconds.
- **Level 2 — Tabs** (stable, URL-addressable: `/clusters/:ns/:name/:tab`): `Overview | Topology | Health | Metrics | Operations | Backups | Configuration | Queries | Events & Logs | Audit`. Grafana/Datadog rule: **all view state lives in the URL** (time range, selected pod, filter chips) so any screen is shareable/bookmarkable — this is disproportionately valued in incident channels.
- **Level 3 — Inspector drawer** (Lens pattern): clicking any pod/PVC/service/secret opens a right-side slide-over with `describe`-style summary, conditions, labels, quick actions (logs, previous logs, restart-runbook), and a raw-YAML toggle — *without navigating away*. Lens proves this halves context loss vs full-page navigation.
- **Breadcrumbs** on every level; **consistent entity chips** (pod chip, cluster chip) that are clickable everywhere they appear (in alerts, timelines, log lines).

## A2. Live topology visualization

ArgoCD's resource tree is the reference implementation: nodes = resources (CR → StatefulSets → Pods → PVCs → Services → Jobs), edges = ownership, each node carries a health icon + short status, updated live. For a Postgres console, do a **domain-specific topology, not a generic k8s tree**:

- Layered left-to-right DAG: clients → pgBouncer tier (n pods) → HA service → primary (crown/star icon) → streaming-replication arrows to replicas (each arrow labeled with live replay lag: `12 MB / 0.8 s`) → repo-host + backup repos (S3/GCS icons) → optional standby-cluster arrow (DR).
- Node color from the status semantics in A9; Patroni role rendered as text (`leader`, `replica`, `sync_standby`) — data straight from `patronictl list -f json` / Patroni `GET /cluster` (members carry `name, role, state, timeline, lag, tags`).
- Auto-layout with dagre/elkjs into plain SVG; do NOT hand-position. Animate only state *transitions* (pulse on role change), not steady state.
- Live feed: server tails `kubectl get pods -l postgres-operator.crunchydata.com/cluster=<c> -w -o json` plus a 10s `patronictl list -f json` poll, merges into one topology document, pushes over the existing SSE lane. Diff on the client and patch the DOM — never re-render the whole graph (causes flicker + lost hover state).
- Clicking an edge (replication link) opens the lag chart for that pair; clicking primary offers the switchover runbook.

## A3. Guided runbooks: preflight → plan/dry-run → confirm → execute/watch → verify → summary

This is the single highest-value pattern for non-Kubernetes users. Synthesis of ArgoCD sync flow, Rancher wizards, Aiven maintenance flows, PlanetScale deploy requests:

1. **Select** — operation catalog with plain-language cards ("Change primary", "Take a backup now", "Restart with new settings", "Restore to a point in time", "Grow disk"). Each card states expected impact up front ("~2 s of write unavailability", "no downtime", "DESTRUCTIVE — overwrites current data").
2. **Preflight** — parallel checks rendered as a checklist with pass/warn/fail + one-line "why". Examples: switchover preflight = target replica streaming, lag < threshold, timeline matches leader, no backup job running, Patroni not paused. Fail blocks Next; warn requires an explicit "proceed anyway" checkbox. Reuse Part B check functions here — same code.
3. **Plan / dry-run** — show the *exact* commands to be run (`kubectl annotate ...`, `kubectl patch ... --type merge -p '...'`) and, for CR mutations, a rendered `kubectl diff` (server-side: `kubectl apply --dry-run=server -f -` to catch CRD/admission validation) — see A6. Copy-button on every command: experts can eject to a terminal; this doubles as k8s education.
4. **Confirm** — impact summary + **typed confirmation** (type the cluster name) for anything destructive (GitHub/PlanetScale danger-zone pattern); a lighter single-click confirm for benign ops. Record "reason" free-text field → goes to audit log.
5. **Execute + Watch** — a phase machine with timestamps (`Submitted → Operator acknowledged → Job created → Rolling pod 2/3 → Complete`), live log pane (SSE), and the relevant metric sparkline inline (e.g. connection count during a restart). Never leave the user staring at a spinner: always show *what evidence* the console is waiting for (e.g. "waiting for pod cluster1-instance1-abcd to become ready, 34 s").
6. **Verify** — automatically re-run the relevant health checks and show before/after (e.g. "primary is now instance2-xyz, timeline 5→6, all replicas streaming, lag 0").
7. **Summary artifact** — persist the full transcript (preflight results, commands, outputs, durations, verifier results) as an immutable run record linked from the activity timeline. This is what makes the console auditable.

Implementation notes: runs execute server-side keyed by run-ID so a closed browser tab doesn't kill an operation (you already have an ops-runs lane — bind the wizard to it); wizard state machine is resumable ("attach to running operation" banner). Guardrail examples: refuse failover when target lag > 64 MB without force; refuse in-place restore unless a backup newer than X exists.

**Operator command cheat-sheet the wizards wrap** (verified):
- Switchover (both operators): patch `spec.patroni.switchover.enabled=true` (+ optional `targetInstance`), then `kubectl annotate --overwrite <kind> <name> postgres-operator.crunchydata.com/trigger-switchover="$(date)"`. Failover variant: `spec.patroni.switchover.type: Failover` (PGO).
- One-off backup: PGO = set `spec.backups.pgbackrest.manual.repoName` (+`options`), annotate `postgres-operator.crunchydata.com/pgbackrest-backup="$(date)"`. Percona v2 = create a `PerconaPGBackup` CR (`spec.pgCluster`, `spec.repoName`, `spec.options`); watch `kubectl get pg-backup <n> -o jsonpath='{.status.state}'` (Starting/Running/Succeeded/Failed).
- Restore: PGO = `spec.backups.pgbackrest.restore.enabled=true` + `repoName` + `options: ["--type=time","--target=..."]`, annotate `postgres-operator.crunchydata.com/pgbackrest-restore=<id>`. Percona v2 = `PerconaPGRestore` CR.
- Rolling restart: PGO documented mechanism = bump any annotation under `spec.metadata.annotations` (propagates to pod template → rolling update); finer-grained: `kubectl exec ... -- patronictl restart <cluster> --pending --force` restarts only members with pending_restart.
- Stop/park cluster: `spec.shutdown: true` (PGO); Percona adds `spec.pause: true` (stop) and `spec.unmanaged: true` (stop reconciling).
- Scale replicas: patch `spec.instances[i].replicas`.

## A4. Command palette (Cmd/Ctrl-K)

Grafana, Datadog, Supabase, GitHub all converged on this. Registry of two entry types: **navigation** ("Open backups — cluster1") and **operations** ("Switchover — cluster1"), where operations only *open the wizard pre-scoped*, never execute directly. Fuzzy match, recency-weighted ranking, keyboard-only operation, entity-scoped sub-search (type cluster name → its actions). Implementation: static action registry + dynamic cluster/pod list injected at open; ~1 day of work with `cmdk`-style list; very high perceived-quality return.

## A5. Activity / audit timeline

Datadog Event Stream + ArgoCD app history synthesis. One merged, filterable stream per cluster (and a global one) mixing four event classes, each visually distinct: (1) console actions (actor avatar, action, params, outcome, link to full run transcript), (2) Kubernetes events (`kubectl get events --field-selector involvedObject...`, dedup by reason+object with count), (3) health-state transitions from Part B ("replication_lag: ok→warning"), (4) operator milestones (backup completed, failover detected via Patroni history). Filter chips: class, severity, actor, time range. Every chart in the console renders these events as **annotation markers on the time axis** (Grafana annotations pattern) — this is the killer feature for "what changed right before latency spiked".

## A6. Diff-before-apply

ArgoCD's live-vs-desired diff is the exemplar. Rules: render unified diff with syntax highlight (side-by-side optional at ≥1280 px); strip noise before diffing (`metadata.managedFields`, `resourceVersion`, `generation`, `creationTimestamp`, `status`); explicitly render "No changes" (don't leave blank); run `kubectl diff -f` for a textual diff *and* `kubectl apply --dry-run=server` for validation errors, and surface admission-webhook rejections verbatim in a red callout; flag restart-requiring parameter changes inline in the diff ("`shared_buffers`: change requires restart — a rolling restart step will be added"). Note `kubectl diff` exit code 1 means "differences found", not error — handle it.

## A7. Undo / rollback affordances

- **Automatic pre-change snapshots**: before every CR mutation, save `kubectl get <cr> -o yaml` (noise-stripped, as in A6) with run-ID, actor, timestamp. Keep a version list per cluster (Grafana dashboard version-history pattern): view any version, diff any two, "Restore this version" — which routes through the *same* diff→confirm→watch pipeline (rollback is just another apply, never a special path).
- Label irreversible operations explicitly: in-place restore, slot drop, `pg_terminate_backend`, cluster delete get red "cannot be undone" framing + typed confirm; everything else advertises its undo ("this can be rolled back from Configuration → History").
- For parameter changes, offer "revert to previous value" directly on the parameter row.

## A8. Empty states & progressive disclosure

Supabase is the benchmark. Every empty tab = 1 sentence of what will appear + 1 primary CTA ("No backups configured for repo2 — Schedule backups") + docs link. Progressive disclosure ladder for non-experts: plain-language verdict first ("Your data is protected — last backup 2 h ago, restore tested never"), then "Details" expands to the structured table, then "Raw" shows actual `pgbackrest info` output / CR YAML. Jargon terms get hover-glossary tooltips (WAL, LSN, timeline, switchover-vs-failover). Wizard defaults chosen so the novice path is always safe; an "expert mode" toggle exposes raw options and skips hand-holding copy (persisted per user).

## A9. Status color semantics

Adopt ArgoCD's proven 6-state model, with icons+text always accompanying color (never color alone):
- **Healthy** — green, filled check.
- **Progressing** — blue, animated ring (any in-flight operation; distinct from unhealthy!).
- **Warning/Degraded-soft** — amber, triangle.
- **Critical/Degraded** — red, octagon; reserve red exclusively for actionable failures (red fatigue is real — Datadog's monitor states discipline).
- **Unknown** — gray, question mark (check couldn't run ≠ failing).
- **Paused/Suspended** — gray-purple, pause icon (`spec.pause`, Patroni paused, `shutdown: true`).
Severity ordering for rollups: critical > warning > unknown > progressing > paused > healthy. One cluster-level rollup chip = max severity of children.

## A10. Real-time charts and streams

- **SSE over WebSockets** for one-directional telemetry (you already have an SSE panel — correct choice): simpler auth (plain HTTP headers/cookies), built-in reconnect with `Last-Event-Id`, proxy/LB friendly. Reserve WebSockets only if you later add interactive terminals.
- Server: per-cluster ring buffer of samples; on SSE connect, first event is a **backfill batch** (last N points) so charts render instantly, then deltas; heartbeat comment every 15–30 s to defeat idle proxy timeouts; per-event `id:` so reconnect resumes without gaps; server-side downsampling above ~2 points/px.
- Client: jittered exponential backoff reconnect + visible "live/stale (reconnecting…)" indicator with last-update age — silently stale charts destroy trust; pause-on-hover with crosshair; **shared cursor + shared time range across all charts on a page** (Grafana); event annotations from A5 on every time axis.
- Latency budget: 5–10 s freshness is fine for everything except the execute/watch phase of a runbook (aim 1–2 s there).

## A11. Dark mode

Tokenize every color as CSS custom properties (`--bg-surface`, `--status-critical`, chart series palette); default from `prefers-color-scheme`, explicit 3-way toggle (system/light/dark) persisted per user; status colors need dark-theme variants re-checked for 4.5:1 contrast against dark surfaces (pure #f00/#0f0 fail); charts need separate gridline/tooltip tokens; test the topology SVG in both themes (hardcoded fills are the usual regression).

## A12. Accessibility (WCAG 2.1 AA floor — enterprise procurement asks for VPAT)

Keyboard reachability for every action incl. topology nodes (tab order + Enter to open inspector); visible focus rings; `aria-live="polite"` on streaming status/log regions, `role="alert"` for new critical findings; status conveyed by icon+text not just color (A9); `prefers-reduced-motion` disables pulse/progress animations; log/terminal panes get proper landmarks and don't trap focus; charts get textual summaries (current value + trend) for screen readers; typed-confirm inputs get explicit labels/descriptions.

## A13. RBAC / SSO expectations (what enterprise buyers screen for)

- **OIDC first** (Authorization Code + PKCE; group/role claim → console role mapping), **SAML 2.0 second** (still mandatory at many older enterprises), SCIM provisioning is the high-end ask. For a harness-embedded console, OIDC + a break-glass local admin account is the credible v1.
- Role model that maps to this domain: `viewer` (read everything, no exec), `operator` (run runbooks), `admin` (edit targets/config, manage users/tokens), `auditor` (audit log only). Enforce **server-side on every endpoint**; UI hiding/disabling is presentation only. Scope roles per cluster-set/namespace (team A can operate cluster1 but only view cluster2) — Rancher's project-scoped RBAC is the model.
- Step-up confirmation (re-auth or typed confirm) for destructive ops; short-lived sessions with refresh; display "you are acting as <role>" in destructive confirms.
- Note: since the console shells out to kubectl with one service credential, the console's RBAC is the *only* authz layer users hit — document that clearly, and consider per-role kubeconfig separation later (read-only kubeconfig for viewer paths) as defense in depth.

## A14. Audit logging requirements (SOC 2 / ISO 27001 driven)

Structured JSON, append-only, one record per (attempted) action: `ts` (RFC3339), `actor` (sub + email), `actor_ip`, `session_id`, `action` (machine-readable verb, e.g. `cluster.switchover.execute`), `target` (cluster/ns/kind/name), `params` (secrets redacted), `outcome` (success/denied/failed), `reason` (user-entered), `run_id`, `duration_ms`, `diff_sha256` (of before/after CR). Log **denials too**. Tamper-evidence: hash-chain each record or ship in near-real-time to an external sink (syslog / webhook / S3 append objects) — "export to SIEM" is the checkbox auditors want. Retention ≥ 1 year (common SOC 2 stance). In-UI: searchable timeline (A5 audit class) + CSV/JSON export. Never log DSNs, passwords, tokens; redact at write time, not display time.

## A15. API & webhook extensibility

- **UI = API client**: every console capability exists as a documented REST endpoint (ArgoCD/Rancher principle — "anything clickable is scriptable"); publish OpenAPI; per-user API tokens with role, expiry, and last-used shown in UI.
- **Outbound webhooks** for: health-state transitions (check, old→new severity, evidence payload), op lifecycle (started/phase/completed/failed), backup completed/failed. HMAC-SHA256 signature header, at-least-once with exponential-backoff retries, dead-letter list visible in UI, per-endpoint event filtering. Slack + PagerDuty templates on top of the generic webhook cover 90% of asks.
- Inbound: a `POST /api/checks/run` and `POST /api/ops/:runbook` make the console composable with CI (e.g. pgbench run gates on health = green).

---

# PART B — Built-in intelligence: implementable health checks

## B0. Execution architecture (kubectl-only)

- **Primary pod discovery** (both operators — Percona keeps Crunchy labels): `kubectl get pods -n $NS -l postgres-operator.crunchydata.com/cluster=$C,postgres-operator.crunchydata.com/role=master -o jsonpath='{.items[0].metadata.name}'`. All-instance pods: label `postgres-operator.crunchydata.com/instance-set`. pgBouncer pods: `postgres-operator.crunchydata.com/role=pgbouncer`. Repo host pod: `$C-repo-host-0` (container `pgbackrest`). DB container name: `database`.
- **SQL runner**: `kubectl exec -n $NS $POD -c database -- env PGOPTIONS='-c statement_timeout=5000 -c application_name=cluster-ops-health' psql -U postgres -d postgres -AtF $'\t' -c "$SQL"` — always with statement_timeout and a distinctive application_name (so the console's own queries are excludable in check #9 and visible in audit).
- **Patroni**: `kubectl exec -n $NS $POD -c database -- patronictl list -f json` (member: `Member/State/Role/TL/Lag in MB`, pending-restart flag) and `patronictl history -f json` (rows: TL, LSN, reason, timestamp, new leader). Patroni REST `GET /cluster` (port 8008 in-pod, via `kubectl exec ... curl -s localhost:8008/cluster`) yields members with `role,state,timeline,lag`.
- **pgBackRest**: `kubectl exec -n $NS $C-repo-host-0 -c pgbackrest -- pgbackrest info --output=json` — JSON is documented-stable; key paths: `.[0].status.code` (0 = ok) & `.status.message`, `.[0].backup[]` with `.type` (full/diff/incr), `.timestamp.{start,stop}` (epoch), `.error` (bool), `.info.{size,delta}`, `.[0].archive[]` `{min,max}` WAL.
- **Cadence tiers**: `fast` = 15–30 s (activity, locks), `standard` = 60 s (connections, replication, slots, archiver, pods, pending_restart, pgbouncer), `slow` = 5 min (checkpoints, temp, autovacuum, backups, PVC samples, patroni history), `daily` (bloat, certs, wraparound can also be hourly). Severity ladder: `ok | info | warning | critical | unknown` — **check execution failure = unknown, never critical**; alert only after 3 consecutive `unknown`. Hysteresis: raise after 2–3 consecutive breaches, clear after 3 consecutive OK; dedupe alert identity = (cluster, check_id, object_key).
- Every finding carries: evidence (the raw rows), threshold that fired, one-line remediation, and — where safe — a **click-action** that opens a pre-filled runbook (never a silent auto-fix).

## B1. Check catalog (ranked; details per check)

### Tier 1 — high value, low effort (one SQL/kubectl call + static threshold; build first)

**1. Connection saturation** — standard (60 s)
```sql
SELECT current_setting('max_connections')::int,
       current_setting('superuser_reserved_connections')::int,
       count(*) FILTER (WHERE backend_type='client backend'),
       count(*) FILTER (WHERE state LIKE 'idle in transaction%')
FROM pg_stat_activity;
```
Ratio = used / (max_connections − reserved). **warn ≥ 0.80, crit ≥ 0.90**; info if idle share > 70% (pooling misconfig hint). Remediation: "Route apps through pgBouncer / raise `max_connections` (restart required)." Click-action: parameter wizard pre-filled `spec.patroni.dynamicConfiguration.postgresql.parameters.max_connections` (flag restart-required); or "kill idle connections" bulk action from evidence rows.

**2. Replication lag** — standard (30–60 s), primary-side
```sql
SELECT application_name, client_addr, state, sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
       COALESCE(EXTRACT(EPOCH FROM replay_lag),0) AS replay_lag_s
FROM pg_stat_replication;
```
Cross-check member count vs `spec.instances[].replicas` (a missing row = replica not streaming = **crit**). Thresholds: **warn ≥ 16 MB or ≥ 30 s; crit ≥ 128 MB or ≥ 300 s** (make configurable; sync_state='sync' member lagging is one level worse). Corroborate with `patronictl list -f json` lag (catches walsender-side blind spots). Remediation: "Inspect replica pod / recovery conflicts; reinitialize a wedged replica." Click-action: "Reinit replica" runbook (`patronictl reinit $CLUSTER $MEMBER --force` with typed confirm) + topology view.

**3. Long-running & idle-in-transaction sessions** — fast (30 s)
```sql
SELECT pid, usename, application_name, state,
       EXTRACT(EPOCH FROM now()-xact_start)::int AS xact_s,
       EXTRACT(EPOCH FROM now()-state_change)::int AS state_s,
       wait_event_type, left(query,120)
FROM pg_stat_activity
WHERE backend_type='client backend' AND pid<>pg_backend_pid() AND state<>'idle'
  AND (now()-xact_start > interval '5 min'
       OR (state LIKE 'idle in transaction%' AND now()-state_change > interval '60 s'));
```
Thresholds: active query **warn ≥ 30 min** (pganalyze's default), crit ≥ 2 h; idle-in-tx **warn ≥ 5 min, crit ≥ 30 min** (holds locks *and* xmin → vacuum/wraparound risk). Remediation: "Cancel/terminate the session; set `idle_in_transaction_session_timeout='5min'`." Click-action: per-row **Cancel** (`SELECT pg_cancel_backend(pid)`) and **Terminate** (`pg_terminate_backend`, typed confirm) buttons; secondary action opens param wizard for the timeout.

**4. Lock waits / blocked queries** — fast (15–30 s)
```sql
SELECT w.pid, w.usename, EXTRACT(EPOCH FROM now()-w.state_change)::int AS wait_s,
       left(w.query,80) AS waiting, b.pid AS blocker_pid, b.state AS blocker_state,
       left(b.query,80) AS blocker_query
FROM pg_stat_activity w
JOIN LATERAL unnest(pg_blocking_pids(w.pid)) bp(pid) ON true
JOIN pg_stat_activity b ON b.pid = bp.pid;
```
Thresholds: any waiter **≥ 30 s warn; ≥ 5 min or ≥ 10 concurrent waiters crit**. Build the chain and highlight the *root* blocker (blocker that is itself unblocked — often idle-in-tx, cross-link check 3). Remediation: "Terminate root blocker; use `lock_timeout` for DDL." Click-action: **Terminate root blocker** with confirm showing its query text.

**5. TXID wraparound distance** — slow/hourly
```sql
SELECT datname, age(datfrozenxid) AS xid_age, mxid_age(datminmxid) AS mxid_age,
       round(age(datfrozenxid)::numeric/2147483647*100,1) AS pct
FROM pg_database ORDER BY 2 DESC;
```
pganalyze-aligned thresholds: **pct ≥ 50% warn, ≥ 80% crit** (of 2^31); also info when max age > 0.9 × `autovacuum_freeze_max_age` (default 200 M) = aggressive autovacuum should already be running. Drill-down query per DB: top tables by `age(relfrozenxid)` from `pg_class`. Remediation: "Find what's blocking xmin advance (long txns, stale slots, prepared xacts), then `VACUUM (FREEZE)` oldest tables." Click-action: opens a "wraparound triage" panel that runs the three blocker queries (checks 3, 6, plus `SELECT * FROM pg_prepared_xacts`) and offers per-table VACUUM FREEZE as a maintenance op.

**6. Inactive replication slots retaining WAL** — standard (60 s)
```sql
SELECT slot_name, slot_type, active, wal_status, safe_wal_size,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;
```
Thresholds: inactive AND retained ≥ 1 GB **warn**; `wal_status IN ('unreserved','lost')` or retained > 25% of PGDATA PVC capacity **crit** (PG13+ has wal_status; 'lost' means a consumer is already broken). Remediation: "Drop the unused slot; set `max_slot_wal_keep_size` as a guardrail." Click-action: **Drop slot** (`SELECT pg_drop_replication_slot('x')`, typed confirm, red irreversible framing — breaks the consumer) + param wizard for `max_slot_wal_keep_size`.

**7. WAL archiving / backup pipeline broken (pg_stat_archiver)** — standard (60 s)
```sql
SELECT archived_count, failed_count, last_archived_wal, last_archived_time,
       last_failed_wal, last_failed_time FROM pg_stat_archiver;
```
**crit** when `last_failed_time > last_archived_time` (archiving currently failing → PITR RPO growing unboundedly and pg_wal will fill); **warn** when failed_count delta > 0 in the window. This is the cheapest highest-value backup check — it fails *hours before* "backup too old" does. Remediation: "Inspect repo-host/pgbackrest connectivity & credentials; check repo storage." Click-action: open repo-host logs + `pgbackrest info` panel.

**8. Backup freshness vs RPO (pgbackrest info + CRs)** — slow (5 min)
Command: `pgbackrest info --output=json` on repo-host (B0). Evaluate per stanza: `.status.code != 0` → **crit** (with `.status.message`); newest `.backup[].timestamp.stop` age > 1.5× configured schedule interval → **warn**, > 2× (or > user-declared RPO) → **crit**; newest backup `.error == true` → warn. Percona extra source: `kubectl get pg-backup -n $NS -o json` → `.status.state == "Failed"` recent → warn. PGO extra: failed Jobs labeled `postgres-operator.crunchydata.com/pgbackrest-backup`. Read schedules from CR `spec.backups.pgbackrest.repos[].schedules.{full,differential,incremental}` (cron). Remediation: "Take an on-demand backup; inspect failed job logs." Click-action: **Backup now** runbook (annotation path for PGO, PerconaPGBackup CR for Percona — you already have both trigger paths).

**9. pending_restart parameter drift** — standard (60 s) + immediately after every config apply
```sql
SELECT name, setting, pending_restart FROM pg_settings WHERE pending_restart;
```
Any row = **warning** ("configuration applied but not in effect"). Corroborate per-member via `patronictl list` pending-restart flag (`*` column / json field) — catches members individually needing restart. Remediation: "Rolling restart to apply." Click-action: **Rolling restart** runbook (prefer `patronictl restart $CLUSTER --pending --force` = restarts only pending members, replicas first; fall back to PGO `spec.metadata.annotations` bump).

**10. Pod restarts / OOMKilled / CrashLoop** — standard (60 s)
`kubectl get pods -n $NS -l postgres-operator.crunchydata.com/cluster=$C -o json` → per container: `restartCount`, `state.waiting.reason=='CrashLoopBackOff'`, `lastState.terminated.reason=='OOMKilled'` (exitCode 137). Thresholds: restartCount delta ≥ 1/15 min **info**, ≥ 3/h **warn**, any OOMKilled of the `database` container in 24 h or CrashLoopBackOff now **crit** (OOM of Postgres = crash recovery + likely failover; correlate with check 17). Remediation: "Raise `spec.instances[].resources.limits.memory`; review `shared_buffers`/`work_mem` sizing." Click-action: open `kubectl logs --previous` viewer + resources wizard.

**11. Checkpoint pressure (requested vs timed)** — slow (5 min), evaluate 1 h deltas
PG ≤ 16: `SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter;` — **PG ≥ 17: the columns moved**: `SELECT num_timed, num_requested FROM pg_stat_checkpointer;` (version-branch the query on `current_setting('server_version_num')`). Threshold on window delta with a floor: total ≥ 4 checkpoints/h AND requested/total ≥ 0.5 → **warn**; ≥ 0.8 → crit-ish warn (perf, not availability — cap at warning). Remediation: "Raise `max_wal_size` (reloadable, no restart); consider `checkpoint_timeout`." Click-action: param wizard `max_wal_size` prefilled with 2× current.

**12. Temp file spill volume** — slow (5 min), 1 h deltas
```sql
SELECT datname, temp_files, temp_bytes FROM pg_stat_database WHERE datname NOT LIKE 'template%';
```
Delta thresholds: **≥ 1 GB/h warn, ≥ 10 GB/h crit** (spills eat the same PVC as PGDATA on these operators). Attribution drill-down if `pg_stat_statements` present: `SELECT queryid, calls, temp_blks_written FROM pg_stat_statements ORDER BY temp_blks_written DESC LIMIT 10;`. Remediation: "Tune the offending query / raise `work_mem` cautiously (per-sort multiplier!); set `log_temp_files=10240`." Click-action: open diagnostics workbench with the attribution query.

**13. Cache hit ratio** — slow (5 min), windowed delta (never lifetime totals)
```sql
SELECT sum(blks_hit) AS hit, sum(blks_read) AS read FROM pg_stat_database;
```
ratio = Δhit/(Δhit+Δread); only evaluate when Δ(hit+read) ≥ 10 000 (noise gate). **< 0.99 info, < 0.95 warn, < 0.90 warn+** for OLTP; label clearly as workload-dependent (analytics scans legitimately tank it) — keep max severity at warning. Remediation: "Raise `shared_buffers` / instance memory; check for new seq scans." Click-action: param wizard `shared_buffers` (restart-required) + workbench "top seq-scan tables" query (`pg_stat_user_tables.seq_scan`).

### Tier 2 — high value, medium effort (needs history storage, extra pods, or parsing)

**14. Autovacuum starvation / dead-tuple pileup** — slow (5 min)
```sql
SELECT schemaname, relname, n_live_tup, n_dead_tup,
       n_dead_tup::float/NULLIF(n_live_tup+n_dead_tup,0) AS dead_ratio,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables WHERE n_dead_tup > 10000
ORDER BY n_dead_tup DESC LIMIT 20;
```
Thresholds: dead_ratio ≥ 0.2 AND n_dead_tup ≥ 100 k **warn**; ≥ 0.4 AND ≥ 1 M **crit**; plus "workers saturated" signal: `count(*) FROM pg_stat_activity WHERE backend_type='autovacuum worker'` == `autovacuum_max_workers` across ≥ 3 consecutive samples → info; stuck vacuums via `pg_stat_progress_vacuum` (phase unchanged 30 min) → warn. Root-cause cross-links: checks 3 (xmin holders) and 6 (slots). Effort is medium because trending last_autovacuum age properly needs per-table history. Remediation: "VACUUM the table now; lower per-table `autovacuum_vacuum_scale_factor` (e.g. 0.01) for hot tables." Click-action: **Vacuum now** maintenance op (`VACUUM (ANALYZE, VERBOSE) schema.table`) + per-table storage-params patch generator.

**15. PVC / WAL disk growth prediction (linear fit to full)** — sample 5 min, predict hourly
Two signal sources, use both: (a) exec-free kubelet stats: `kubectl get --raw "/api/v1/nodes/$NODE/proxy/stats/summary"` → `.pods[].volume[]` `usedBytes/capacityBytes` (match by PVC name `$POD-pgdata`); (b) in-pod `df -B1 /pgdata` + `SELECT sum(size) FROM pg_ls_waldir();` + `SELECT pg_database_size(datname) FROM pg_database;` to split "data vs WAL vs temp" growth. Store (ts, used) in sqlite; least-squares over trailing 6 h and 24 h windows; ETA_to_95% = (0.95·cap − used)/slope (report the *worse* of the two windows; suppress when slope ≤ 0). Severity: used ≥ 80% **warn**, ≥ 90% **crit**; ETA < 7 d **warn**, < 48 h **crit**. WAL-specific: if `pg_ls_waldir()` sum > 2× `max_wal_size` → dedicated warning "WAL not being released" and auto-cross-link checks 6/7 (slots/archiver are the usual culprits — this compound finding is gold for non-experts). Remediation: "Expand PVC via `spec.instances[].dataVolumeClaimSpec.resources.requests.storage` (needs `allowVolumeExpansion` on the StorageClass) or clear the WAL-retention cause." Click-action: **Grow disk** runbook (checks StorageClass expansion support in preflight) — you have PVC plumbing already.

**16. pgBouncer pool exhaustion** — standard (60 s)
Signal: `SHOW POOLS` (`cl_active, cl_waiting, sv_active, sv_idle, maxwait, maxwait_us, pool_mode`) + `SHOW STATS` deltas. Access path (medium effort, verify at runtime): exec into a pgbouncer pod (`-c pgbouncer`) and connect to the admin DB `pgbouncer` as a user listed in `stats_users` — both operators provision a stats user for their exporter (name/password live in the operator-managed pgBouncer secret, e.g. `<cluster>-pgbouncer`; enumerate keys with `kubectl get secret <c>-pgbouncer -o json | jq '.data|keys'` rather than hardcoding). Thresholds: `cl_waiting > 0` for 3 consecutive samples **warn**; `maxwait ≥ 1 s` warn, `≥ 5 s` **crit**; `sv_active == default_pool_size` sustained + waiters → "pool exhausted" compound finding. Distinguish from check 1: server saturated vs pooler saturated (the fix differs!). Remediation: "Raise `default_pool_size`/`max_client_conn` via `spec.proxy.pgBouncer.config.global`; add pgBouncer replicas." Click-action: config patch wizard + pgBouncer-only rolling restart.

**17. Patroni timeline churn / unexpected failovers** — slow (5 min)
`patronictl history -f json` → array of [TL, LSN, reason, timestamp, new_leader]. Persist last-seen TL; **each unseen increment = an event on the activity timeline** ("Failover/switchover to X at T, reason: …"). Thresholds: > 1 TL increment/24 h **warn**, ≥ 3/24 h **crit** (flapping); member whose TL ≠ leader TL in `patronictl list` → warn ("needs reinit"). Correlate automatically with check 10 (OOM at same timestamp ⇒ "failover caused by OOM" compound finding). Remediation: "Investigate cause (OOM, liveness probe, node pressure); reinit stale members." Click-action: open failover timeline view with pod-event overlay; **Reinit member** runbook.

**18. Certificate expiry** — daily
Enumerate operator TLS secrets: `<cluster>-cluster-cert`, replication cert secret, operator root CA (e.g. `pgo-root-cacert`), plus any `spec.customTLSSecret`/`customReplicationTLSSecret`. For each: `kubectl get secret $S -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -enddate` (iterate all certs in bundle: `openssl crl2pkcs7 -nocrl -certfile - | openssl pkcs7 -print_certs`). **< 30 d warn, < 7 d crit, expired crit.** Note: operator-managed certs self-rotate — finding text must distinguish "operator will rotate; watch it" (info at < 30 d) from custom certs ("you must rotate", warn/crit). Remediation: "Rotate customTLSSecret; then rolling restart." Click-action: cert inspector panel listing all cluster certs + ages.

**19. Operator/CR status & member-state rollup** — standard (60 s)
`kubectl get perconapgcluster $C -o json` / `kubectl get postgrescluster $C -o json`: surface `.status.state` (Percona: e.g. initializing/ready/paused) and PGO `.status.conditions[]` where `status != "True"` for positive-polarity types (e.g. Progressing stuck > 15 min → warn; PersistentVolumeResizing active → progressing badge). Patroni member `state NOT IN ('running','streaming')` → **crit**; Patroni cluster paused → paused badge (suppress failover-related checks while paused to avoid noise). Cheap, and it's what makes the topology statuses truthful.

### Tier 3 — valuable but higher effort / lower certainty (build last)

**20. Bloat estimates** — daily
Use the check_postgres/ioguix estimation queries (pure-SQL, no extension; store as canned queries — they're ~60 lines, ship them as files) for table + btree bloat. Thresholds: bloat_pct ≥ 30% AND wasted ≥ 1 GB **warn**; ≥ 50% AND ≥ 5 GB **crit**. Offer exact measurement on demand via `pgstattuple` if the extension is available (`CREATE EXTENSION pgstattuple` is in contrib on both operators' images). Estimation is fuzzy — always label "estimated". Remediation: index bloat → "`REINDEX INDEX CONCURRENTLY`" (safe click-action runbook, PG12+); table bloat → "pg_repack (not bundled in operator images) or `VACUUM FULL` (exclusive lock)" — text-only suggestion, no click-action for VACUUM FULL beyond an expert-mode maintenance op with heavy warnings.

**21. Query-level attribution (pg_stat_statements)** — on-demand + hourly snapshot
Not a check per se but the drill-down layer several checks want (12, 13). Detect availability (`SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'`), offer one-click enable via param wizard (`shared_preload_libraries` — restart required) + `CREATE EXTENSION`. Then: top by `total_exec_time`, `mean_exec_time`, `temp_blks_written`, `shared_blks_read`.

**22. Restore drill / backup verifiability** — weekly, expert-triggered
Highest-trust signal an ops console can produce: spin an ephemeral restore (Percona: new cluster with `dataSource` from repo; PGO: clone via `spec.dataSource.postgresCluster`) into a scratch namespace, verify `pg_isready` + row-count probe, tear down, record "last verified restore: date". Big effort; flagship differentiator later.

## B2. Value/effort ranking (build order)

| Rank | Check | Value | Effort | Rationale |
|---|---|---|---|---|
| 1 | 7 archiver failing | very high | trivial | earliest warning for both disk-full and RPO loss |
| 2 | 2 replication lag | very high | low | core HA truth; feeds switchover preflight |
| 3 | 1 connection saturation | very high | low | most common outage cause in pgbench-style load |
| 4 | 9 pending_restart | high | trivial | directly powers your config-apply verify stage |
| 5 | 3 long/idle-in-tx | high | low | actionable click-kill; root cause for 5,14 |
| 6 | 4 lock waits | high | low | incident triage; pairs with 3 |
| 7 | 6 stale slots | high | trivial | classic silent disk-filler |
| 8 | 10 pod restarts/OOM | high | low | pure kubectl; explains failovers |
| 9 | 8 backup freshness | very high | medium-low | JSON parse + schedule math |
| 10 | 5 wraparound | high (rare but fatal) | low | pganalyze-proven 50/80% thresholds |
| 11 | 15 disk growth prediction | very high | medium | needs history store; compound WAL finding is a standout feature |
| 12 | 19 CR/member rollup | high | low | makes topology honest |
| 13 | 11 checkpoints | medium | low | version-branched query, pure tuning signal |
| 14 | 12 temp spills | medium | low | delta math only |
| 15 | 17 TL churn | medium-high | medium | history persistence + correlation |
| 16 | 16 pgbouncer pools | medium-high | medium | auth path needs runtime discovery |
| 17 | 14 autovacuum starvation | medium | medium | per-table trending |
| 18 | 13 cache hit ratio | low-medium | low | noisy; cap at warning |
| 19 | 18 cert expiry | medium | medium | mostly self-healing under operators |
| 20 | 20 bloat | medium | high | fuzzy estimates, heavy queries |
| 21 | 21 pg_stat_statements layer | high | medium | drill-down enabler, not a check |
| 22 | 22 restore drill | very high (trust) | very high | later flagship |

## B3. Cross-cutting implementation notes

- **Compound findings beat raw checks for non-experts**: implement a tiny rules layer that merges e.g. (WAL growing) + (slot inactive) → single card "Disk is filling because replication slot `x` is holding 12 GB of WAL — fix: drop the slot", with the click-action from the causal check. Same for (failover) + (OOMKilled) and (dead tuples) + (idle-in-tx holding xmin). This is what pganalyze/Datadog do well and generic dashboards don't.
- **Every threshold user-tunable** per cluster with sane defaults above; store overrides with audit trail; a "silence this finding for N hours/with reason" affordance (PagerDuty/Datadog muting norm) prevents alert fatigue.
- **Version awareness**: branch on `server_version_num` for pg_stat_checkpointer (≥ 170000), `wal_status` in pg_replication_slots (≥ 130000), `mxid_age` (≥ 9.5, fine); operators currently ship PG 13–18.
- **Never auto-execute remediations.** Click-actions open the corresponding pre-filled runbook (Part A3) with the same preflight/confirm pipeline — the health engine suggests, the runbook engine acts, the audit log records.


<!-- MACHINE_CATALOG -->
```json
{"execution":{"primary_pod_selector":"postgres-operator.crunchydata.com/cluster=$C,postgres-operator.crunchydata.com/role=master","sql_runner":"kubectl exec -n $NS $POD -c database -- env PGOPTIONS='-c statement_timeout=5000 -c application_name=cluster-ops-health' psql -U postgres -AtF '\\t' -c $SQL","severities":["ok","info","warning","critical","unknown"],"hysteresis":{"raise_after":3,"clear_after":3},"cadences_s":{"fast":30,"standard":60,"slow":300,"daily":86400}},"checks":[{"id":"archiver_failing","rank":1,"source":"sql","cadence":"standard","query":"SELECT archived_count,failed_count,last_archived_time,last_failed_time FROM pg_stat_archiver","critical":"last_failed_time > last_archived_time","warning":"failed_count delta > 0","remediation":"Fix pgBackRest repo connectivity/credentials","click_action":"open_repo_host_logs"},{"id":"replication_lag","rank":2,"source":"sql+patronictl","cadence":"standard","query":"SELECT application_name,state,sync_state,pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn),COALESCE(EXTRACT(EPOCH FROM replay_lag),0) FROM pg_stat_replication","warning":"lag>=16MB or >=30s","critical":"lag>=128MB or >=300s or expected replica missing","remediation":"Inspect replica pod; reinit wedged replica","click_action":"runbook:patronictl_reinit"},{"id":"connection_saturation","rank":3,"source":"sql","cadence":"standard","query":"SELECT current_setting('max_connections')::int,current_setting('superuser_reserved_connections')::int,count(*) FILTER (WHERE backend_type='client backend'),count(*) FILTER (WHERE state LIKE 'idle in transaction%') FROM pg_stat_activity","warning":"used/(max-reserved)>=0.80","critical":">=0.90","remediation":"Route via pgBouncer or raise max_connections (restart)","click_action":"param_wizard:max_connections"},{"id":"pending_restart","rank":4,"source":"sql+patronictl","cadence":"standard","query":"SELECT name,setting FROM pg_settings WHERE pending_restart","warning":"any row","remediation":"Rolling restart (patronictl restart --pending)","click_action":"runbook:rolling_restart_pending"},{"id":"long_idle_tx","rank":5,"source":"sql","cadence":"fast","query":"pg_stat_activity filter: xact>5min or idle-in-tx>60s (see report B1.3)","warning":"active>=30min or idle_in_tx>=5min","critical":"idle_in_tx>=30min or active>=2h","remediation":"Cancel/terminate; set idle_in_transaction_session_timeout","click_action":"row:pg_cancel_backend|pg_terminate_backend"},{"id":"lock_waits","rank":6,"source":"sql","cadence":"fast","query":"pg_blocking_pids join (see report B1.4)","warning":"wait>=30s","critical":"wait>=5min or waiters>=10","remediation":"Terminate root blocker; lock_timeout for DDL","click_action":"terminate_root_blocker"},{"id":"stale_slots","rank":7,"source":"sql","cadence":"standard","query":"SELECT slot_name,active,wal_status,pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn) FROM pg_replication_slots","warning":"inactive and retained>=1GB","critical":"wal_status in (unreserved,lost) or retained>25% PVC","remediation":"Drop unused slot; set max_slot_wal_keep_size","click_action":"drop_slot_confirm"},{"id":"pod_restarts_oom","rank":8,"source":"kubectl","cadence":"standard","query":"kubectl get pods -l ...cluster=$C -o json -> restartCount, lastState.terminated.reason","warning":">=3 restarts/h","critical":"OOMKilled(database) in 24h or CrashLoopBackOff","remediation":"Raise memory limits; check shared_buffers sizing","click_action":"logs_previous+resources_wizard"},{"id":"backup_freshness","rank":9,"source":"pgbackrest+cr","cadence":"slow","query":"pgbackrest info --output=json on $C-repo-host-0; kubectl get pg-backup (percona)","warning":"age>1.5x schedule or last backup error","critical":"status.code!=0 or age>2x schedule/RPO","remediation":"Trigger on-demand backup; inspect job logs","click_action":"runbook:backup_now"},{"id":"txid_wraparound","rank":10,"source":"sql","cadence":"slow","query":"SELECT datname,age(datfrozenxid),mxid_age(datminmxid) FROM pg_database","warning":"pct>=50 of 2^31","critical":"pct>=80","remediation":"Unblock xmin (long txns/slots/prepared), VACUUM FREEZE oldest","click_action":"wraparound_triage_panel"},{"id":"disk_growth","rank":11,"source":"kubelet_stats+sql+history","cadence":"slow","query":"/api/v1/nodes/$NODE/proxy/stats/summary volumes; df -B1 /pgdata; SELECT sum(size) FROM pg_ls_waldir()","warning":"used>=80% or ETA<7d","critical":"used>=90% or ETA<48h; WAL>2x max_wal_size -> cross-link slots/archiver","remediation":"Expand dataVolumeClaimSpec storage or clear WAL retention cause","click_action":"runbook:grow_disk"},{"id":"cr_member_rollup","rank":12,"source":"kubectl+patronictl","cadence":"standard","query":"CR .status.state/.status.conditions; patronictl list member state","critical":"member state not running/streaming","info":"paused/progressing badges","remediation":"n/a (status truth layer)","click_action":"topology_inspector"},{"id":"checkpoint_pressure","rank":13,"source":"sql","cadence":"slow","query":"pg<17: pg_stat_bgwriter checkpoints_timed/req; pg>=17: pg_stat_checkpointer num_timed/num_requested","warning":"req/total>=0.5 with >=4 cpts/h (delta)","remediation":"Raise max_wal_size (reloadable)","click_action":"param_wizard:max_wal_size"},{"id":"temp_spills","rank":14,"source":"sql","cadence":"slow","query":"SELECT datname,temp_files,temp_bytes FROM pg_stat_database","warning":">=1GB/h delta","critical":">=10GB/h","remediation":"Tune query / raise work_mem cautiously; log_temp_files","click_action":"workbench:pgss_temp_attribution"},{"id":"tl_churn","rank":15,"source":"patronictl","cadence":"slow","query":"patronictl history -f json; persist last TL","warning":">1 TL/24h or member TL != leader TL","critical":">=3 TL/24h","remediation":"Investigate failover cause; reinit stale members","click_action":"failover_timeline_view"},{"id":"pgbouncer_pools","rank":16,"source":"pgbouncer_admin","cadence":"standard","query":"SHOW POOLS (cl_waiting,maxwait,sv_active); auth via operator pgbouncer secret stats user","warning":"cl_waiting>0 x3 or maxwait>=1s","critical":"maxwait>=5s or pool exhausted","remediation":"Raise default_pool_size/max_client_conn via spec.proxy.pgBouncer.config.global","click_action":"pgbouncer_config_wizard"},{"id":"autovacuum_starvation","rank":17,"source":"sql","cadence":"slow","query":"pg_stat_user_tables n_dead_tup/dead_ratio/last_autovacuum; pg_stat_progress_vacuum","warning":"ratio>=0.2 and dead>=100k","critical":"ratio>=0.4 and dead>=1M","remediation":"VACUUM now; per-table scale_factor; unblock xmin","click_action":"vacuum_now+storage_params_patch"},{"id":"cache_hit","rank":18,"source":"sql","cadence":"slow","query":"delta of sum(blks_hit),sum(blks_read) FROM pg_stat_database (gate: delta>=10000)","info":"<0.99","warning":"<0.95 (cap at warning)","remediation":"Raise shared_buffers/memory; check seq scans","click_action":"param_wizard:shared_buffers"},{"id":"cert_expiry","rank":19,"source":"kubectl+openssl","cadence":"daily","query":"secrets <c>-cluster-cert etc -> openssl x509 -enddate","warning":"<30d (custom certs)","critical":"<7d or expired","remediation":"Operator certs self-rotate; rotate customTLSSecret + rolling restart","click_action":"cert_inspector"},{"id":"bloat","rank":20,"source":"sql_estimation","cadence":"daily","query":"check_postgres/ioguix estimation queries; optional pgstattuple on demand","warning":">=30% and >=1GB","critical":">=50% and >=5GB","remediation":"REINDEX CONCURRENTLY (indexes); pg_repack/VACUUM FULL (tables, locks)","click_action":"runbook:reindex_concurrently"}],"operator_ops":{"switchover":{"both":"spec.patroni.switchover.enabled+targetInstance then annotate postgres-operator.crunchydata.com/trigger-switchover=$(date)"},"backup_now":{"pgo":"spec.backups.pgbackrest.manual + annotate postgres-operator.crunchydata.com/pgbackrest-backup=$(date)","percona":"PerconaPGBackup CR (pgv2.percona.com/v2) spec.pgCluster+repoName; watch .status.state"},"restore":{"pgo":"spec.backups.pgbackrest.restore + annotate postgres-operator.crunchydata.com/pgbackrest-restore=<id>","percona":"PerconaPGRestore CR"},"rolling_restart":{"pgo":"bump spec.metadata.annotations","fine":"patronictl restart $CLUSTER --pending --force"},"stop":{"pgo":"spec.shutdown:true","percona":"spec.pause:true; spec.unmanaged:true to stop reconciling"}}}
```
