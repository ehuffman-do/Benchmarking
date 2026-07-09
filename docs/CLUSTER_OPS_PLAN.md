# Cluster Ops Module — Orientation, Gap Analysis & Implementation Plan

Status: **awaiting approval — no feature code has been written.**
Scope: port of the field-tested bash cluster-ops methodology (failover probes,
backup impact, CR tuning, kubeconfig-driven operations) into `pgbench-harness`
as a first-class "Cluster Ops" module.

---

## 1. Orientation — what the codebase gives us today

A short inventory of the existing infrastructure this module will stand on,
with file references, so the gap analysis below is checkable.

| Infrastructure | Where | State |
|---|---|---|
| Job queue | `jobs` table (`db.py:49`), `queries.claim_next_job`, worker loop (`worker.py:276`) | Mature. `kind` is already multi-valued (`run`, `soak`, `preflight`, `prepare`, `doctor`); the worker claims jobs, runs each in a thread with its own SQLite connection, honors admin-set concurrency. |
| Web/worker separation | `worker.py` module docstring; separate systemd service | Mature. The web tier only enqueues; the worker shells out to the `pgbench-harness` CLI as a session-leader child; cancel = SIGTERM the process group with SIGKILL escalation (`worker.py:338`). Bouncing the web tier never touches a run. |
| Secret handling | `secrets_store.py` (Fernet, refs-only in DB), redactor (`pgbench_harness/util.py:44`), env-injection at exec (`worker.py:89–102`) | Mature. Exactly the pattern Part 0 requires: plaintext only in worker memory and child env; redactor scrubs any secret registered with it from all captured output. |
| SSE live streaming | `_sse` (`app.py:965`), `_job_sse` (`app.py:1105`) | Mature. Pattern is file-tailing with offsets: the worker/harness writes files under the run dir; the web tier tails them into SSE events. No message bus — trivially extensible to new file layouts. |
| RBAC / CSRF / headers | `ROLE_RANK` viewer<operator<admin (`app.py:41`), `require(min_role)`, `_check_csrf`, security-headers middleware (`app.py:137`) | Mature. |
| Audit | `queries.audit(conn, username, action, target, detail)`; audit page + CSV export | Mature; just needs calls on every new route. |
| Results/index invariant | `results/` = truth; SQLite `runs` = index (`index.py`) | Mature, but `_run_row` assumes the *benchmark* layout (`manifest.json` + `spec.yaml`). Ops runs need their own index (see gaps). |
| Saved targets pattern | `targets` table + CRUD routes + `password_ref` into secret store | Mature. Exact template for **Kube Targets**. |
| Config templates + diff | `templates` table, `/api/diff` (difflib, `app.py:874–905`) | Present. Reusable for parameter-bundle templates; CR-value diffing is new logic. |
| Background sampler pattern | `LivePgSampler` (`capture.py:998`): thread → `parsed/pg_timeseries.csv` → SSE → chart | Mature. Template for backup samplers and the Part 4 monitor. |
| Report machinery | Jinja2 + uPlot templates in `pgbench_harness/templates/`, `report_soak.interactive_payload` | Mature for benchmarks; ops reports need new templates in the same style. |
| Fake-binary test shim | `tests/fakebin/{psql,sysbench}` PATH-injected, env-var-scriptable failure modes | Exact precedent for the **fake-kubectl shim**. |
| Leak tests | CLI: `test_no_password_anywhere_in_results` (`test_e2e.py:189`, rglob over the run dir); web: `test_secret_never_leaks_anywhere` (`test_webapp.py:432`, rglob over the whole data dir + API/HTML echo checks); write-time redaction (`test_report_unit.py:52`) | The gates to extend with kubeconfig + k8s-derived-password sentinels. |
| External-provider integration | `provider.py` (DO API metrics, token in secret store) | Precedent for optional integrations that degrade gracefully. |
| SPA | `frontend/src` — React 18 + react-router (basename `/ui`), pages (History, NewRun, Targets, Tasks, JobView, RunDetail, Compare, LiveCompare, ReportView, Diagnostics, Users, Settings, Audit), role-filtered nav (`Shell.tsx:11–26`), `lib/sse.ts` offset-aware EventSource wrapper, uPlot via `LiveChart`/`AnnotatableChart` | Mature; needs a new "Cluster Ops" nav section + pages. Build output is committed into `static/spa/` (no Node needed on the droplet); no frontend test runner exists (typecheck only). |
| Deploy | `deploy.sh` idempotent installer; systemd units in `packaging/systemd/` (worker: `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=/var/lib/pgbench-harness …`) | Needs kubectl added to `install_packages` and a sanctioned kubeconfig directory (see risks §5.9). No CI exists in the repo — the verification story stays `pytest` + `tsc --noEmit`. |

**Architecture takeaway:** the harness's execution model — *web writes a spec +
enqueues; worker shells out to a first-class CLI; the CLI owns the run dir; the
web tier tails files into SSE* — fits Cluster Ops without modification. The
plan below keeps that shape: ops runs are driven by new
`pgbench-harness ops …` CLI subcommands so we inherit cancellation, crash
recovery, redaction, and web-restart-immunity for free.

## 2. Gap analysis — per capability

Legend: **EXISTS** = existing infra, needs only a new job type / UI wiring.
**PARTIAL** = pattern exists, real new code needed. **NET-NEW** = built from scratch.

### Part 0 — Kubeconfig management

| Capability | Status | Notes |
|---|---|---|
| Kubeconfig path registration + browser upload | **NET-NEW** (storage pattern EXISTS) | New `kube_targets` table modeled on `targets`. Uploaded copies go through the secret store (Fernet) or 0600 files under `data_dir/kubeconfigs/`; path-references stay where the user put them. |
| Kubeconfig validation (`kubectl version`, `get ns` / `auth can-i`) | **NET-NEW** | New kubectl subprocess layer (`KUBECONFIG` in child env only). Because the web tier must never run kubectl, validation is a **fast worker job** (new kind `ops-validate`), surfaced in the UI via the existing `_job_sse` pattern. |
| Kube Target registry (context, ns, CR kind/name, pguser secret, db user/name) | **PARTIAL** | Table + CRUD + secret-ref pattern copied from `targets`; auto-discovery of CR name and pguser secret is new logic in the discovery job. |
| Secret non-leakage (kubeconfig + k8s-derived DB password) | **EXISTS** (extension) | Redactor + env-injection already do this for DB passwords. New: register kubeconfig contents and any secret-derived password with the redactor in every ops process; extend the leak tests to grep for both. |
| Topology discovery (pods, leader via `patronictl list -f json`, STS, services, pgBackRest info, schedules) | **NET-NEW** | Python parsing (no grep/heredoc). Runs as job kind `ops-discover`; result cached as JSON in SQLite (last-known topology) + returned live. |
| Audit + admin-only + typed confirmation | **EXISTS** (wiring) | `require("admin")`, `queries.audit`, CSRF all present. Typed cluster-name confirmation = new server-side check comparing the submitted name to the target's CR name. |
| `results/ops/<run_id>/` layout + index | **PARTIAL** | Filesystem convention is new (`raw/`, `TIMELINE.txt`, `events.csv`, `meta.json`, `report.html`); indexing needs a new `ops_runs` table — `index._run_row` is benchmark-specific and must not be overloaded. |

### Part 1 — CR configuration operations

| Capability | Status | Notes |
|---|---|---|
| Parameter bundle templates (Patroni params, pgBackRest globals) | **PARTIAL** | Ship as built-in editable templates; the `templates` table pattern fits, but these are structured JSON patches, not spec YAML — store as a new template kind or built-in constants + user overrides. |
| Dry-run: exact merge patch + diff vs current CR | **NET-NEW** | Read CR (`kubectl get -o json`), compute value-level diff in Python; show patch JSON verbatim. `/api/diff` (text difflib) is a fallback display, not the mechanism. |
| Apply via `kubectl patch` + verify loop (`pg_settings` on leader, `pending_restart` detection, `patronictl show-config`) | **NET-NEW** | Job kind `ops-cr-apply`. Loud failure on `pending_restart = t` with the "operator will roll pods → expect failover" warning surfaced in UI and report. |
| pgBackRest rendered-config verification (grep in pod) | **NET-NEW** | `kubectl exec … grep -rE … /etc/pgbackrest/` run from Python, parsed in Python. |
| Prep actions (reset checkpointer stats, drop/recreate bench DB) | **NET-NEW** | Optional steps of the same job; version-appropriate `pg_stat_reset_shared` call. |
| CR snapshot + one-click rollback (as a new patch) | **NET-NEW** | Snapshot full CR YAML into the op run dir before patching; rollback computes a parameters-only patch from the snapshot — never a blind `kubectl apply`. |

### Part 2 — Backup operations

| Capability | Status | Notes |
|---|---|---|
| Trigger via direct exec and via operator `manual:` block, tracked to completion | **NET-NEW** | Job kind `ops-backup`; path recorded in `meta.json` + report. |
| `--from leader` / `--from replica` (+ `backup-standby=y`), record which node did the work, dual-node load sampling | **NET-NEW** | Leader resolution reuses the discovery layer's patronictl parsing. |
| Lock preflight (abort on held stanza lock — the rc=50 false-success fix) | **NET-NEW** | Checked before firing *and* reused by Part 3's safety rails. |
| Schedule deconfliction: view, pause (snapshot + remove), restore; stray `manual:` and active-Job checks | **NET-NEW** | Pause/restore are admin actions with typed confirmation; "schedules paused" nag state stored in SQLite so the UI nags until restored. |
| Samplers every 5s → CSV per sampler (`pg_stat_archiver` with the empty-result fix, archive queue depth via `archive_status/*.ready`, pgbackrest info before/after, log tail) | **PARTIAL** | `LivePgSampler` is the structural template (thread → CSV), but all cluster-side collectors are new, kubectl-exec-based, parsed in Python with unit tests against captured real output. |
| `meta.json` with UTC ISO + epoch-ms window keys | **NET-NEW** | Trivial but load-bearing (alignment key for correlation). |
| Benchmark-run correlation (op run ↔ live soak/sweep on same target): linked runs, overlay report with shaded backup window, archive-queue secondary axis, before/during/after KPI deltas | **PARTIAL** | Both runs are indexed in the same SQLite and both have per-second CSVs + epoch anchors; the overlay/report section is new, but the LiveCompare/uPlot payload machinery gives the chart plumbing. |

### Part 3 — Failover scenarios

| Capability | Status | Notes |
|---|---|---|
| Scenario runs (Cases A, B, C1, C2) with baseline → FIRE → settle orchestration | **NET-NEW** | Job kind `ops-scenario`; one CLI process owns all capture children in its process group (so existing cancel semantics reap everything). |
| Capture streams: 5 Hz write probe through pgBouncer (separate write + `SELECT clock_timestamp(), pg_is_in_recovery(), inet_server_addr()`, `-q`, no `INSERT…RETURNING`), Patroni/pgBouncer logs per pod, pod/event watches, `fire.marker`, optional public-endpoint probe | **NET-NEW** | All raw-logged under `raw/`. Probe timestamps come from `clock_timestamp()` in-database, never shell `date`. |
| Stitcher (stitch.py port): one UTC axis, `TIMELINE.txt` + `events.csv` + JSON; downtime decomposition; pgBouncer backoff tail; **leader-name-based flip classification** (not IP); T7 ready-count-dip latch fix; probe-artifact windows | **NET-NEW** | Pure-Python module in `pgbench_harness/ops/`, unit-tested against captured real fixtures. This is the highest-value port; the bug fixes are encoded as tests first. |
| Live view (probe OK/FAIL, leader, member states, event feed over SSE) | **EXISTS** (pattern) | New SSE generator tailing the scenario run dir's status files — same offset-tailing mechanism as `_sse`. |
| Per-scenario report + cross-scenario comparison table | **PARTIAL** | New Jinja2 templates in the existing report style; comparison view follows the existing `compare` machinery shape. |
| Safety rails: admin-only, typed confirmation, refuse if backup lock held, refuse concurrent scenarios per target | **PARTIAL** | RBAC/confirmation wiring exists; lock preflight comes from Part 2; per-target concurrency guard is a new queue-side check (`jobs` active on same kube_target). |

### Part 4 — Telemetry monitor

| Capability | Status | Notes |
|---|---|---|
| Per-target continuous sampler (WAL MB/s, checkpoints, `.ready` vs recycled, archive rate, lag bytes+time, disk), leader re-detected per cycle, split queries (no one-CTE blanking) | **PARTIAL** | `LivePgSampler` structure + SSE panel pattern exist; cluster collectors are new. Needs a design decision on how a long-lived monitor coexists with the job queue (see Decisions). |
| Scale-event watching | **OUT OF SCOPE** | Noted as a future module. |

### Cross-cutting

| Capability | Status | Notes |
|---|---|---|
| Fake-kubectl shim for e2e tests | **PARTIAL** | `tests/fakebin/` PATH-injection precedent; new scripted kubectl with canned per-subcommand outputs + scenario scripting (state transitions during a simulated Case B). |
| Leak-test extension | **EXISTS** (extension) | Add kubeconfig-content and k8s-derived-password sentinels to the existing rglob gates. |
| Installer/deploy/docs | **PARTIAL** | deploy/packaging must add the kubectl prerequisite check; OPERATIONS.md/README gain the kubeconfig flow, safety model, and exact fire commands. |

## 3. Proposed architecture (the one big structural decision)

**Ops runs are driven by new first-class CLI subcommands** under
`pgbench-harness ops …` (implemented in a new `pgbench_harness/ops/` package),
not by Python executed inside the worker process:

```
pgbench-harness ops validate  --ops-spec job.yaml            # kubeconfig check
pgbench-harness ops discover  --ops-spec job.yaml            # topology snapshot
pgbench-harness ops cr-apply  --ops-spec job.yaml [--dry-run]
pgbench-harness ops backup    --ops-spec job.yaml
pgbench-harness ops scenario  --ops-spec job.yaml            # case A|B|C1|C2
pgbench-harness ops monitor   --ops-spec job.yaml            # phase 5
```

Rationale (same reasons the benchmark worker shells out):
- **Cancel/crash semantics for free.** The CLI is the process-group leader; the
  existing SIGTERM/SIGKILL group stop reaps every capture child (kubectl
  logs -f, probes, samplers). On SIGTERM the ops runner finalizes (stitches
  what it has, restores anything it paused where safe).
- **Web-restart immunity** unchanged.
- **CLI parity for debugging**: every scenario can be run by hand on a laptop
  against a kubeconfig, exactly like the bash scripts today.
- The worker gains only a small dispatch: ops job kinds build an
  `ops` argv instead of a benchmark argv, and set `KUBECONFIG` (and register
  kubeconfig contents + derived passwords with the redactor) in the child env.

Ops job specs are YAML files written by the web tier (like benchmark specs):
kube-target reference resolved to non-secret fields (context, namespace, CR
name…) + operation parameters. **Never** kubeconfig contents or passwords.

Data model additions (forward-only migrations 4+):
- `kube_targets` (name, kubeconfig_path *or* kubeconfig_ref, context,
  namespace, cr_kind, cr_name, pguser_secret_name/key, db_user, db_name,
  created_utc) — secrets by reference only.
- `ops_runs` index table (op_run_id, kind, kube_target_id, scenario/backup
  params summary, status, linked_bench_run_id, created/finished, headline
  metrics like downtime_ms / peak_queue).
- `jobs.kube_target_id` column + new `kind` values; `jobs.options` (exists)
  carries per-op parameters.
- `settings`/state entry for the "schedules paused" nag per target.

Results layout: `results/ops/<op_run_id>/{meta.json, raw/*, TIMELINE.txt,
events.csv, stitched.json, report.html, cr_snapshot.yaml, …}` — raw streams are
truth; stitched outputs are derived and regenerable (`ops stitch --run-dir`).
Benchmark-index safety: `index.reconcile` and `worker._run_dir_names` only
treat a directory as a run if it contains `manifest.json`, so the `results/ops/`
subtree is invisible to the existing benchmark index — no regression risk from
sharing `results/`.

## 4. Implementation plan & phasing

I agree with the prescribed 5-phase order — it matches the dependency graph
(kubectl layer → CR ops → backups → scenarios → monitor) and each phase is
independently shippable. Adjustments I propose:

- **Phase 1 splits into two reviewable increments** (1a: kubectl exec layer +
  kube targets + validation + leak tests; 1b: discovery + topology panel +
  `results/ops` layout + audit wiring). Phase 1a is where all the security
  plumbing lands and deserves its own review.
- **The stitcher lands early in Phase 4, before the scenario runner** — tests
  against your captured fixtures first, runner second. The stitcher encodes the
  hard-won classification lessons; building it against real fixtures de-risks
  the whole phase.
- **Backup lock preflight is built in Phase 2's kubectl layer vocabulary but
  ships in Phase 3** (as specified) and is exposed as a reusable check so
  Phase 4's safety rail is the same code.

### Phase 1 — Foundation (read-only)
1a: `pgbench_harness/ops/kube.py` (kubectl subprocess wrapper: env-only
KUBECONFIG, timeouts, JSON parsing, redaction hooks); `kube_targets` table +
CRUD API + UI form (path field + optional upload); `ops validate` job kind +
live check UI; redactor extension; **leak-test extension (kubeconfig sentinel +
fake k8s-secret password)**; fake-kubectl shim v1.
1b: `ops discover` (pods, patronictl JSON parsing, STS, services, pgBackRest
info, CR schedules); topology panel (refreshable, cached last-known); `results/ops/`
layout + `ops_runs` index + ops-run list/detail pages; audit on everything.
*Tests:* unit (patronictl/pgbackrest parsers on captured output), e2e (shim-backed
validate + discover), leak gate, RBAC on new routes.

### Phase 2 — CR configuration
Parameter bundles as built-in editable templates; `ops cr-apply` with
`--dry-run` (patch JSON + value diff) and apply→verify loop (`pg_settings`,
`pending_restart` loud-fail, `patronictl show-config` view); pgBackRest global
options with rendered-config verification; CR snapshot before patch + rollback-
as-new-patch; prep actions. Admin-only + typed confirmation.
*Tests:* shim-backed dry-run/apply/verify e2e including a `pending_restart=t`
scenario and a rollback round-trip.

### Phase 3 — Backups
`ops backup` with both execution paths and `--from leader|replica|<pod>`;
lock preflight (abort-on-lock e2e test); schedule view/pause/restore with
snapshot + persistent nag; samplers (archiver — with the non-empty-parse unit
test against real captured output — queue depth, pgbackrest info before/after,
log tail); `meta.json` epoch-ms window; report; **benchmark-run correlation**
(link + combined overlay section + KPI deltas).
*Tests:* sampler parsers on real fixtures; shim e2e full/diff/incr both paths;
lock-abort; schedule pause/restore round-trip; correlation overlay payload.

### Phase 4 — Failover scenarios
Stitcher module first (fixtures → tests → port); then `ops scenario` runner
(capture-stream supervisor, baseline/settle timing, fire.marker, Cases A, B,
C1); classification + downtime decomposition + backoff tail in report; live SSE
cockpit; cross-scenario comparison; safety rails (lock preflight reuse, per-
target scenario mutex, typed confirmation). **Case C2 last, flagged
experimental** pending field validation.
*Tests:* stitcher unit tests on your real run dirs (classification, T7 dip,
backoff math, probe-artifact windows); shim-backed simulated Case B end-to-end.

### Phase 5 — Telemetry monitor
`ops monitor` long-running sampler (split queries, per-cycle leader re-detect),
CSV + SSE panel, overlay source for backup/failover reports. Ships only after
1–4 are solid.

Each increment: migration + API + worker dispatch + UI + tests + docs update in
the same PR-sized change; installer/OPERATIONS.md updated in the phase that
introduces the need (kubectl prerequisite in 1a).

## 5. Riskiest / highest-effort pieces (flagging now)

1. **Scenario capture-stream supervision (Phase 4)** — one runner owning ~6–10
   concurrent kubectl children (logs -f per pod, watches, probes) for the run
   duration, surviving pod churn mid-scenario (log streams die when the pod they
   follow is deleted — must auto-reattach). Highest engineering-effort piece.
   Doing it right (reattach + gap flagging) costs materially more than a naive
   "start streams, hope" port — I recommend doing it right; your call.
2. **Stitcher fidelity without a live cluster** — the classification and latch
   fixes are only as good as the fixtures. **I need your raw run directories
   from past captures** (offered in the prompt) before Phase 4 starts; ideally
   at least one restart-in-place, one real election, one with the pgBouncer
   backoff tail, and the T7 stale-sample case.
3. **Case C2 (node loss)** — least characterized in the field, most destructive,
   and DOKS node deletion semantics (cordon/drain vs delete) interact with the
   cluster autoscaler. Built last, flagged experimental, needs live validation.
4. **Operator-path backup tracking** — PGO `manual:` annotation/Job semantics
   vary across 2.x minors; direct-exec path is the well-trodden one. I'll build
   direct-exec first and treat operator-path as its own tested increment.
5. **Schedule pause/restore touches the user's CR** — a crash between pause and
   restore leaves schedules off. Mitigations: snapshot-before-pause in the run
   dir *and* SQLite nag state that survives restarts, restore-on-finalize in the
   runner's SIGTERM path, and a visible banner until restored.
6. **Clock skew between app host and cluster** — probe timestamps are
   in-database (good), but k8s events/logs use cluster clocks. The stitcher must
   record and display the measured skew (app-host vs `clock_timestamp()`) rather
   than assume zero.
7. **Patroni/PGO output drift** — `patronictl list -f json` and pgbackrest info
   formats across versions. Parsers get versioned fixtures; discovery records
   the versions it saw into `meta.json`.
8. **SSE volume at 5 Hz probe + N log streams** — the live cockpit needs
   decimation/rollup (status summaries, not raw firehose); raw stays on disk.
9. **systemd sandboxing vs kubeconfig paths** — the worker unit runs with
   `ProtectHome=true` + `ProtectSystem=strict` (`packaging/systemd/
   pgbench-worker.service`), so a kubeconfig the user copies to `/root` or
   `/home/...` is *invisible* to the worker, not merely unreadable. The
   sanctioned location must be `data_dir/kubeconfigs/` (already inside
   `ReadWritePaths`); validation must detect the sandbox case and say exactly
   that instead of a generic "file not found". Installer creates the directory
   (0700, `pgbench`-owned) and OPERATIONS.md documents the copy step.
   `install_packages` in `deploy.sh` gains kubectl (pinned upstream binary).

## 6. Decisions I need from you

1. **Ops runner as CLI subcommands** (`pgbench-harness ops …`, §3) vs Python
   inside the worker process. I strongly recommend the CLI for cancel/crash
   parity — confirm.
2. **kubectl subprocess only** (no Python k8s client) — per your constraint the
   subprocess is the default; I see no need for the library in Phases 1–4
   (watches use `kubectl get -w`/`kubectl get events -w`). Confirm.
3. **Separate `ops_runs` index table** rather than widening `runs` — keeps the
   benchmark index untouched (no regression risk). Confirm.
4. **Discovery/validation as fast worker jobs** — the "web tier never runs
   kubectl" constraint means even read-only topology refresh goes through the
   queue (results in ~2–5 s via the existing job-SSE path, cached last-known
   topology shown instantly). Alternative: a narrow exemption letting the web
   tier run *read-only* kubectl. I recommend the queue for uniformity — but it
   makes the topology panel async. Your call.
5. **Kubeconfig storage default**: path-reference (file stays where the user
   put it, app validates readability) with browser-upload storing an encrypted
   copy under the data dir. Both supported; path-reference is the primary flow
   per your description — **but** note risk §5.9: under the shipped systemd
   hardening, path-referenced files are only visible under
   `/var/lib/pgbench-harness/`, so the docs/UI will steer users to
   `data_dir/kubeconfigs/`. Confirm.
6. **Phase 5 monitor execution model**: a long-lived monitor occupies a worker
   concurrency slot indefinitely under the current queue. I propose a separate
   "monitor lane" (dedicated thread pool, not counted against benchmark
   `max_concurrency`). Needs your OK when we get there — flagging now.
7. **Fixtures**: please supply the raw run directories (failover captures incl.
   an election, a restart-in-place, backoff-tail case; a backup-impact run;
   sample `patronictl list -f json` / `pgbackrest info` outputs from PGO 2.9 /
   PG 18) before Phase 4 — Phase 1 parsers can start from whatever you can
   share earliest.
8. **Public-endpoint probe (Part 3 optional)**: fine to defer to a Phase 4
   follow-up increment? It's additive and independent.

## 7. Verification commitments (per your spec)

- Stitcher unit tests against real captured fixtures: classification
  (restart-in-place vs election), T7 dip fallback, backoff-tail math,
  probe-artifact windows.
- Fake-kubectl shim (PATH-injected, canned + scripted state transitions)
  driving e2e tests: discovery, CR patch/verify (incl. `pending_restart`),
  backup preflight abort-on-lock, simulated Case B end-to-end.
- Extended leak gate: kubeconfig sentinel string + k8s-secret-derived password
  must appear in no DB row, job spec, log, SSE payload, report, or artifact.
- `pg_stat_archiver` parser non-empty assertion against captured real psql
  output (the field bug).
- Installer/docs updates per phase; manual smoke checklist per phase for the
  first live-cluster run.
