# Changelog

## Unreleased — bug bash round 5 (whole-harness adversarial review)

- **Knee-finder ladder accounting rewritten**: after a queue-full skip the
  ladder REBASES (the forced step gets its full window anchored at the next
  segment's start) instead of being re-derived from wall-clock — the old
  math re-ran the forced step and booked a spurious relaunch per healthy
  re-run, which burned max_relaunches, forced "partial" status and polluted
  the report with fake restart events. Relaunch accounting now keys on the
  previous segment's actual outcome: a crash landing past a step boundary
  counts (it used to evade the budget), a healthy completion never does,
  and queue-full skips are a datum, not an outage. Queue-full detection
  scans the whole segment log (the 5-line excerpt could bury it).
- **Soaks are stoppable**: a PID-directed SIGTERM only set a flag that was
  checked between segments — a single-segment week-long soak would ignore
  it for days. The handler now terminates the current load generator so
  the partial-report finalize runs immediately.
- **Disk guard now guards**: the free-space check ran once per segment —
  once at t=0 for a healthy non-stepped soak. It now re-checks every ~60s
  during the stream and cleanly aborts (results-so-far intact) instead of
  hitting ENOSPC days in; and a harness-side tee failure (ENOSPC on the
  log write) kills the child instead of orphaning a load generator that
  keeps hammering the target for up to --time.
- **Suite aborts keep the completed cells**: SIGTERM/Ctrl-C mid-matrix (and
  a pgbench-init failure) finalize the real per-level status (partial, not
  failed-wholesale), emit the bundle best-effort, and no longer leave the
  manifest stuck 'running' with an orphaned child. run/suite post-
  processing (parse + report) is best-effort after a completed run — a
  report bug can no longer flip a multi-day result into a failed job.
- **Parsers**: numeric regexes no longer accept malformed tokens ("12..3"
  from a torn log splice crashed finalize); pgbench "lat NaN" progress
  lines (a stalled interval — exactly the sample not to lose) parse as 0.
- **Watchdog grace capped** at 15 min (was duration/2 — 12h tolerance for a
  hung child in a 24h cell); a typo'd PGB_LEVEL_WATCHDOG_GRACE_S is ignored
  instead of crashing at level start.
- **soak.report_interval_s must be 1**: the downtime/TTR model is strictly
  per-second dense; any coarser interval scored ~(1-1/N) of a flawless run
  as outage. Explicitly rejected now.
- **Worker: recycled PIDs can no longer be adopted** — orphan reattach and
  Cancel now verify process identity via /proc start time, not just
  os.kill(pid,0): after a droplet reboot a recycled pid used to become a
  phantom "running" job that starved the queue, and Cancel could SIGKILL an
  unrelated process group. A non-UTF-8 byte in child output no longer
  abandons a live benchmark (errors=replace), and any worker-side failure
  now terminates the benchmark process group instead of orphaning it with
  the job marked failed.
- **Worker: decrypted kubeconfig copies are swept** — a worker restart
  mid-job skipped the normal cleanup, leaving the plaintext kubeconfig on
  disk indefinitely; startup now sweeps copies whose job is no longer
  running, and reattach convergence unlinks its own.
- **Cockpit stream correctness**: the live-CSV tail detects the harness's
  atomic finalize/resume rewrite by inode and tells the client to rebuild
  (was: torn rows or thousands of duplicate points in the "final" chart);
  CRLF row terminators are stripped; the task-output stream tails by byte
  offset (was O(file) per second) and drains the final lines that land
  with the terminal state flip (they used to vanish).
- **Backup/failover safety rails fail CLOSED**: an exec failure during the
  pgBackRest lock check used to read as "lock clear" — the harness could
  fire a backup or failover into a running backup, the exact field bug the
  check exists to prevent. "Cannot verify" now aborts with the reason.
- **Report regeneration survives cross-version/partial data**: manifests
  with unknown keys (newer harness, hand annotations) no longer TypeError
  every report entry point; older soak summaries missing newer keys no
  longer KeyError the recovery path; the webapp now routes suite/probe
  runs to the evidence renderer (the sweep renderer 500'd on them) and
  the sweep renderer raises a real error instead of an assert.
- **Failover stitch is derived data**: a stitcher exception downgrades to
  a warning event instead of flipping a successful scenario run to failed
  (captures are intact on disk).
- **Kubeconfig redaction parses the document**: JSON kubeconfigs (every
  line quoted) and YAML block scalars evaded the line-regex — ZERO values
  were registered with the redactor for those formats. The sensitive keys
  are now found by structured walk, with the line scan as fallback; an
  empty kubeconfig path is rejected instead of silently falling back to
  ~/.kube/config (wrong-cluster risk).
- **Small but real**: a "tps": null in a soak summary no longer makes the
  whole run vanish from the index; string-valued tags no longer explode
  into per-character tags; ops liveness treats EPERM as alive; an unknown
  health severity no longer aborts postprocess; kubectl timeouts keep the
  child's last output as the diagnosis; the connection-ceiling probe reaps
  killed psql children (was: zombies for the process lifetime).
- **Web tier**: run artifact downloads spool to disk instead of building
  a potentially multi-GB tar.gz in RAM; /runs/{id}/provider-metrics gets
  the same traversal guard as every other run route; SSE streams no longer
  pin an unused SQLite connection for their lifetime; malformed
  kube_target_id / scheduled_utc are clean 400s (a bad scheduled_utc used
  to make the job silently permanently ineligible).

## Unreleased — device-probe iteration (field fixes from the first live probe)

- **Device probe is now a first-class New Run mode** (admin-only): threads,
  async backlog, IO pattern (rndrw/rndrd/rndwr), duration, file geometry and
  keep-files are form fields — no more pasting YAML. Cluster pages
  quick-launch all three patterns with the cluster pre-attached, and the
  form refuses to submit without an attached cluster. The knee-finder's
  seeded rate ladder now starts low (100…2000, 0) — unachievable steps skip
  forward, but a ladder that opens far beyond capacity wastes its first
  segments.

- **Verdicts say WHEN and DURING WHAT**: the first live EXCEEDS verdict
  (12,540 IOPS sustained) turned out to sit in sysbench's end-of-run fsync
  flush — a large-write regime — while the steady random phase served
  ~7.4K; only PMM could show that. The verdict now stamps the sustained-
  peak window's UTC timestamps and attributes it to the phase event that
  contains it ("[peak window 04:36:41–04:36:51 UTC, during 'fileio run']"),
  and the device probe stamps its phases (prepare / run / done) into
  events.jsonl like every other run mode already did. A peak that butts
  against the next phase marker is additionally flagged as a possible
  flush/transition burst.

- **`device_probe.keep_files`**: preparing the fileio test set took ~5 min
  for 100 GB on the live cluster and was repeated on every probe run. With
  `keep_files: true` the probe skips prepare when the files already exist
  under `/pgdata/pgb-fileio-probe/` and skips cleanup at the end, so
  probe iterations (more threads, deeper backlog, rndrd vs rndwr) start in
  seconds. The free-space guardrail relaxes accordingly when reusing.
  A final run with `keep_files: false` (or a manual
  `rm -rf /pgdata/pgb-fileio-probe` in the pod) reclaims the space.
- **"fileio result: ?" fixed by device-derived fallback**: some sysbench
  builds print a summary format our regexes don't match, leaving the
  evidence bundle without probe figures. When summary parsing yields
  nothing, the probe now derives reads/s, writes/s, total IOPS and MB/s
  from the device counter series over the run window and labels the
  source, so the verdict engine and report always get real numbers
  (the raw sysbench output is still kept in `raw/fileio_run.log`).

## Unreleased — longevity hardening (hours-to-week-long runs)

- **Cell watchdog for sweep/suite/pgbench**: a hung load generator
  (connected but silent) froze a run forever — soak always had a watchdog,
  the sweep path did not. Every cell now has a wall-clock ceiling
  (duration + grace); a hung child is killed, the level marked failed with
  "harness watchdog killed the load generator", and the run continues.
- **Deploy-safe long runs**: worker unit gets `KillMode=process`; benchmark
  children (own process groups) survive worker restarts, and the restarted
  worker re-attaches by PID (startup reconcile) and converges the job + run
  index when the child eventually finishes. `deploy.sh --update` mid-run no
  longer kills a week-long benchmark.
- **Sampler resilience**: the live PG sampler survives per-iteration
  exceptions instead of dying silently for the rest of the run; the device
  IOPS stream auto-respawns with backoff when the exec dies (token refresh,
  failover, network blips) — gaps stay visible, never interpolated — and
  now streams only the sampled device's diskstats line (megabytes per week,
  not gigabytes).
- **Cockpit scales to week-long series**: the SSE stream tails CSVs
  incrementally by byte offset (was: re-parse the whole file every second
  per viewer — pathological past a few hours) with a capped backfill of the
  most recent ~6 h on (re)connect (`reset` flag; full history stays in the
  CSVs/report), and the browser keeps a rolling window instead of unbounded
  arrays. The page also rebuilds a dead EventSource on network-online /
  tab-wake, so laptop sleep or moving houses just reconnects.
- **Disk-space guard**: sweep/suite/soak check free space between cells/
  segments — low space warns once, critically low stops the run CLEANLY
  with results-so-far intact, instead of corrupting artifacts at ENOSPC.
- **Reports: full DB-settings capture restored everywhere** — the suite/
  probe evidence report and the soak report now include the key-settings
  table + the full pg_settings dump (the classic sweep report always had
  it), so provider-vs-provider settings diffs (DO Advanced vs Aiven) work
  from any run's report. Raw CSV remains env/pg_settings.csv in the bundle.


## Unreleased — field fixes from the first live PMM enablement

- **HTTP 401 from the PMM inventory API is no longer reported as "server
  unreachable"** — a status code is an answer. 401/403 now reads "PMM API
  rejected the token" with an actionable pointer (check the service-account
  token/role in the PMM UI; the agents use the same value). Still a
  warning, never a run failure.
- **`libs MISSING` false alarm hardened**: the runtime
  `shared_preload_libraries` probe on a freshly bounced/elected leader now
  retries for up to 60s and surfaces the real psql error when it ultimately
  fails (an empty answer no longer silently reads as "libraries missing"),
  and library matching normalizes quotes/spaces/operator-doubled entries the
  way real clusters render the value.


- **Single pane of glass**: the whole framework runs from the console. New
  Run gains a **suite mode** form (ladder/duration/pgbench toggle), an
  **io_stress** workload form (dataset_gb / mix / key distribution), and
  **rate steps** on soak; an "Attach cluster" selector lists the registered
  Kube Targets — attaching one makes the worker inject that target's
  kubeconfig AND auto-synthesizes the spec's `cluster:` section from the
  registry, so storage identity + the device-IOPS series are captured with
  zero YAML editing. The cluster page gains an **IOPS evidence** quick-launch
  card (suite / rate-stepped, pre-attached). The run page shows the
  **verdict banner** (capped / exceeds / inconclusive) served by the new
  `/api/runs/{id}/evidence` endpoint, next to the existing bundle download.
  Device-probe specs submit through New Run too (admin-only, requires an
  attached cluster, plus the in-spec `allow_device_probe` arming the runner
  enforces); web dry-run renders suite and probe plans.

- **New run modes**: `suite` (the storage team's full evidentiary matrix —
  oltp_point_select / read_only / read_write / write_only + pgbench TPC-B and
  SELECT-only across a thread ladder, sequential segments, one consolidated
  bundle), rate-stepped soak (`soak.rate_steps` + `step_duration_s` via
  sysbench `--rate`, each step stamped as an event), and `device-probe`
  (guardrailed sysbench fileio from a pod pinned to the primary's node,
  mounting the pgdata PVC — refuses without `allow_device_probe: true`,
  checks free space >= 2x file size, cleans up files + pod on all exit paths).
- **`workload.type: io_stress`**: `dataset_gb` as the primary knob (table_size
  derived; sized to defeat caches), `mix: read|write|mixed` picks the stock
  lua, `rand_type` configurable (uniform default).
- **pgbench as a second driver**: command builders + progress/summary parsers
  mirroring the sysbench module; `doctor` checks the binary.
- **Cluster-aware evidence** (spec `cluster:` section, KUBECONFIG env-only):
  storage identity capture (PVC/PV/StorageClass/placement with high-IOPS
  marker detection — "no marker" recorded as evidence) and a 1s device IOPS
  series (single long-lived exec streaming /proc/diskstats; device resolved
  from /proc/self/mountinfo; derivation to reads/writes/IOPS/MB/s/await/util/
  queue in summarize). Both degrade to recorded warnings, never run failures.
- **Verdict engine**: configurable `limits:` (recorded, not hardcoded);
  10s-sustained peak + utilization -> **capped / exceeds / inconclusive**
  with the numbers and, for inconclusive, what stopped scaling. Printed in
  CLI output and the report.
- **Evidence bundle**: report.html mirroring the storage team's structure
  (verdict, storage identity, per-workload exact-SQL descriptions generated
  from workload definitions, peak + per-concurrency tables, scaling charts,
  device timeline with reference-limit lines + event marks, auto-populated
  caveats), plus evidence.json and CSV series; the existing web "Artifacts"
  download ships it as one self-interpreting archive.


## Unreleased — PMM bug bash (round 3): seven fixes

- **Bounce is now genuinely HA-preserving**: the sidecar bounce deletes
  replicas first and the leader LAST (only after every replica is back),
  and each pod's wait compares against the UID captured *just before its
  delete* — the old wait compared against the stale pre-patch snapshot and
  treated an absent (deleted) pod as done, so it could pass before the pod
  was even recreated.
- **pmm-disable restore is conflict-safe**: the backed-up CR is sanitized
  before re-apply (server-owned metadata — resourceVersion, uid,
  creationTimestamp, generation, managedFields — and status stripped), and
  the run now actually watches the operator shed the pmm-client sidecars
  (headline `reconciled`; timeout = warning) instead of printing a wait it
  never performed.
- **rollback_of path traversal closed** at both layers: the web route
  rejects ids with separators/dots (400) and the runner independently
  aborts (`bad-rollback-id`) — a crafted id can no longer point the restore
  at arbitrary filesystem paths.
- **Token hygiene**: `PGB_PMM_TOKEN` is stripped of surrounding whitespace
  (a pasted trailing newline used to corrupt the Bearer header AND the
  secret) ; the inventory check refuses non-HTTP(S) `server_host` schemes
  (e.g. `file://`) instead of passing them to urlopen.
- **pmm-status no longer sleeps**: the QAN wait loop slept up to 10 s even
  in no-wait mode — every status run paid it; now capped by the actual
  remaining deadline (zero for status).
- **Crash-proof finalization**: pmm-status/pmm-disable now catch unexpected
  exceptions like pmm-enable does — a stray error can no longer strand a
  run in "running" forever.
- **Operator UX**: the PMM Dry-run button is admin-only (the enable route
  always was, so operators got a confusing 403). Regression tests for all
  of the above, plus double-enable idempotence and the cross-op 409 mutex
  (a queued PMM enable blocks a backup and vice versa). Suite: 284.

## Unreleased — PMM 3.x enablement as a first-class operation

- **New: `ops pmm-enable`** — takes a Percona PostgreSQL cluster from
  unmonitored to fully PMM-3.x-monitored in one run (a native port of the
  field `enable-pmm.sh`, not a shell-out): preflight (token, query-source ↔
  extension pairing, CR exists) → pre-change topology → full **state backup**
  (CR, secret, patronictl config/list, per-pod PMM env, preload libs +
  extensions, with a one-line restore command) → PMM3 secret
  (`PMM_SERVER_TOKEN` key — the wrong key silently means PMM2 mode) → a
  **single CR merge patch** (pmm block + `shared_preload_libraries`) →
  rollout wait → re-discovery → `CREATE EXTENSION` on the primary →
  HA-preserving sidecar bounce → per-node/cluster validation report →
  **server-side confirmation** against the PMM inventory REST API
  (unreachable server degrades to a warning). `ops pmm-status` re-runs just
  the validation with zero mutations; `ops pmm-disable` restores the
  backed-up CR and deletes the secret.
- **Two reference-script bugs fixed in the port, not reproduced**:
  the rollout wait is **spec-aware** (a pod only counts as rolled when it is
  Running + Ready *and* carries the patched spec *and* was actually
  recreated — pods merely Ready on the old spec no longer fool the wait;
  timeouts continue to verification with a recorded warning), and leader
  discovery is **retry-with-deadline resilient** (`discover.
  resolve_leader_resilient`: operator role label first — no exec — then
  patronictl against *every* running pod; election windows and dying exec
  targets are retryable; the deadline error carries every attempt).
- **Token hygiene**: the PMM service-account token comes ONLY from
  `PGB_PMM_TOKEN`, is registered with the output redactor, travels to
  kubectl via an `apply -f -` stdin manifest (never argv), renders as
  `<token>` in dry-run, and a test greps every file the harness writes to
  prove it never lands anywhere. Non-`glsa_` tokens warn, not fail.
- **Benchmark ↔ observation linkage**: run specs accept an optional `pmm:`
  section (`server_host`, optional `service_name`); sweep and soak reports
  then include PMM deep links (instances overview + Query Analytics) scoped
  to the run's exact time window. Specs without `pmm:` are byte-identical.
- **PMM from the console** — new "PMM monitoring" panel on the cluster page:
  server host + query source (extension paired automatically), dry-run,
  one-click Enable (admin + typed cluster-name confirmation + the shared
  one-destructive-op-per-target mutex), read-only Check status (operator),
  and Disable (auto-restores the CR snapshot from the newest enable run).
  The sidecar state badge comes from the last topology snapshot; the token
  stays worker-side only (`PGB_PMM_TOKEN`) and never transits the browser.
- **Preload libraries are preserved, not replaced**: `pmm-enable` now
  auto-detects the cluster's existing `shared_preload_libraries` (CR spec
  first, live runtime on the leader as fallback) and appends the PMM
  extension with order-preserving dedupe — a cluster running
  `pgaudit,pgvector,pg_cron` ends up with
  `pgaudit,pgvector,pg_cron,pg_stat_monitor`, and the validation report now
  verifies every preserved library, not just the extension.
  `params.base_libs` remains as an explicit override.
- **Deploy: worker secrets file** — `deploy.sh` now creates
  `/etc/pgbench-harness.secrets.env` (0600, root-only, created once and
  never overwritten on update) and the worker unit loads it via an optional
  `EnvironmentFile=-` line. Put `PGB_PMM_TOKEN` there — not in the 0644
  `/etc/pgbench-harness.env`, which is world-readable and regenerated on
  every deploy. Documented in OPERATIONS.md ("Worker secrets").

## Unreleased — day-2 operations catalog + continuous intelligence

- **New: guided operations** (`ops operate` + console → target → Operations):
  rolling **restart** (cluster-wide via the operator's annotation channel, or
  a single member via patronictl — wired to the pending_restart health
  finding's "inspect" button), everyday **switchover/failover** with a
  target picker (preflight refuses leaderless/no-replica states),
  **scale replicas** (JSON-patch + watch until new members stream;
  scale-down warns PVCs are deleted), **vertical resize** (preflight warns
  when the memory limit is under 2× shared_buffers — the OOM-loop classic),
  and a **backup schedules & retention editor** (cron-validated, verified
  against the CR). Every operation: preflight → dry-run plan → typed
  confirmation → live watch → verify; one destructive op per target.
- **Continuous intelligence**: per-target **auto health checks** (off/15m/1h/6h,
  scheduled by the worker), a bounded **health history** (migration 7) with a
  new /health-history API, **transition notifications** (ok→warn→crit and
  recoveries; state changes only, never repeats) through the existing
  Slack/email channels, and a **disk-fill trend projection** — a linear fit
  over history that raises "~N days to 90%" as a warn (<14 d) or crit (<3 d)
  finding long before the static threshold fires.
- **pgBouncer click-to-apply**: new `pgbouncer_global` cr-apply action
  patching `proxy.pgBouncer.config.global` with dry-run diff and a verify
  loop against the rendered ini in the pgbouncer pod (SIGHUP reload — no
  restart); the pgBouncer catalog tab is now stageable like pgBackRest.
- **Cluster overview**: the target page opens with KPI tiles (leader,
  healthy members, timeline, health) plus **sparklines** for WAL rate, data
  volume fill, and replica lag fed from the latest monitor run's CSVs.

## Unreleased — console redesign (single pane of glass)

- **Sidebar shell** — grouped left navigation (Observe / Benchmarking /
  Cluster Ops / Administration) with icons, active rail, user + theme +
  logout in the footer; slim topbar; collapses to a slide-over on narrow
  screens. Content area widens to 1400px.
- **Unified Runs feed** — the Runs page now shows benchmark runs AND
  cluster-ops runs in one sorted feed with kind chips, per-kind result
  summaries (peak QPS vs downtime/checks/cycles), kind filter segs, and
  correct per-kind links. Fixes the "no runs found" dead end when clicking
  an ops run (e.g. a monitor) from the runs page — active ops jobs now link
  to /ops/runs/… instead of /runs/….
- **Command palette (⌘K / Ctrl-K)** — jump to any page, cluster (including
  its parameter map / diagnostics), or recent run of either kind; arrow-key
  navigation; also reachable from the sidebar search button.
- **Breadcrumbs** on Cluster Ops subpages (Clusters / target / Parameter
  map|Diagnostics).
- **Table overflow fixes** — long CR paths, option names, and descriptions
  now wrap inside their columns (sidecar catalogs use fixed column layout +
  anywhere-wrapping); wide tables scroll inside their card instead of
  punching through the page.

## Unreleased — Cluster Ops intelligence layer

- **New: Parameter map** (`ops pg-params` + console page) — the full
  `pg_settings` catalog introspected live from the Patroni leader (names,
  values, types, units, min/max, enum values, contexts, descriptions,
  pending_restart), overlaid with the Patroni apply channel
  (cr / dcs-coordinated / patroni-locked / readonly) and CR-managed value
  provenance. Searchable + filterable console page with typed editors
  (validation derived from the server), staged changes → dry-run / apply &
  verify through the existing cr-apply loop. Snapshot cached per target
  (migration 6).
- **New: Diagnostics workbench** (`ops diag` + console page) — 17 curated
  read-only checks (sessions, locks, replication, slots, wraparound, cache
  hit, dead tuples, sizes, temp spill, checkpoints, WAL, patronictl,
  pgBackRest inventory, pods, warning events, PVC usage) streaming CSVs into
  the live cockpit; watch mode re-samples live checks on an interval for
  moving charts. Operator-level; nothing mutates.
- **New: Health checks** (`ops health` + target panel) — threshold heuristics
  producing findings with severity, one-line remediation, and a deep-link
  action (to the matching diagnostic or parameter filter); worst severity
  cached and badged on the targets list; thresholds overridable per run.
- **Replica backups steered correctly** — Source=replica locks the trigger
  path to the operator (repo-host) flow; the form shows whether
  `backup-standby: "y"` is set and offers a one-click enable; the runner
  aborts replica+direct with the rc=56 explanation instead of failing
  mid-run.
- **Sidecar option catalogs** — research-curated maps of every
  operator-relevant pgBackRest option (119), Patroni DCS setting (31), and
  pgBouncer ini option (68), each with type/default/allowed values and the
  exact CR path it applies through on Percona v2 vs Crunchy v5; new tabs in
  the Parameter map page with search; pgBackRest global options are
  click-to-apply through the existing dry-run/verify loop.
- **Replica backups without a CR change** — the operator trigger path now
  adds a one-off `--backup-standby=y` to the manual options when the source
  is a replica, so a single standby-sourced backup needs no global CR edit
  (schedules still need `backup-standby: "y"` in the global map).
- **Research + roadmap** — eight source-verified research reports under
  `docs/research/` (Percona v2 CRD surface, Crunchy v5 CRD, PostgreSQL
  parameter internals, sidecar options, ranked day-2 operations catalog,
  replica-backup mechanics, enterprise UX + intelligence patterns, and a
  completeness critique), synthesized into `docs/CLUSTER_OPS_ROADMAP.md`.
- **Target management polish** — edit an existing Kube Target from the
  console (including kubeconfig replacement; switching back to path mode
  drops the stale imported copy), validation records a pass/fail verdict
  (migration 5) shown in the targets list and reset on edit, and discover
  now surfaces the real kubectl/auth error instead of "no CR found".

## Unreleased — Cluster Ops module

- **New: Cluster Ops** — kubeconfig-driven operations for Kubernetes-hosted
  PostgreSQL (Percona PG Operator v2.x) as first-class `pgbench-harness ops`
  subcommands + a new console section: Kube Target registration/validation,
  topology discovery (patronictl parsed in Python), CR configuration with
  dry-run diff / verify loop / pending_restart loud-fail / snapshot+rollback,
  pgBackRest backups (direct + operator paths, leader/replica sources, lock
  preflight, 5s samplers, schedule pause/restore with nag), failover
  scenarios (switchover / pgkill / pod-delete / node-loss-experimental) with
  a fixture-tested stitcher (leader-name classification, T7 dip latch,
  pgBouncer backoff tail, probe-artifact windows), continuous telemetry
  monitor with per-cycle leader re-detection, self-contained ops reports +
  cross-scenario comparison, live SSE cockpits. Secrets model extended:
  KUBECONFIG child-env injection, redactor-registered kubeconfig credentials
  and k8s-secret passwords, leak test extended. Worker gains a monitor lane
  (ops_monitor never consumes benchmark concurrency). Installer adds kubectl
  and the sanctioned `kubeconfigs/` directory.


## Unreleased — operator console (incremental)

- **Removed automatic anomaly detection.** The harness no longer auto-detects or
  marks anything on the timeline (no more `dip` / `scale_down` / `error_burst` /
  `latency_spike` annotations). Only **operator-marked** events are stamped on the
  charts/tables of both the interactive and classic reports. Dropped the `detect`
  module, the `detected` summary field + its report sections, and the unused
  `report.detect_*` spec knobs. The always-on run profile (median TPS, CoV,
  outages) is unchanged.
- **Removed spec-declared timeline events.** A soak spec can no longer
  pre-declare events (the `events:` section, including `at_s` timing) — an
  `events:` key is now rejected with a hint. Timeline events come only from the
  analysis's **auto-detection** or from **operator marks** (the live cockpit /
  report stamping, or `pgbench-harness mark`). **Breaking:** remove any
  `events:` section from existing soak specs/presets.
- **Switched query-stats capture from pg_stat_statements to pg_stat_monitor.**
  The `capture.pg_stat_statements` spec key is now `capture.pg_stat_monitor`
  (same `auto`/`true`/`false` semantics); preflight detects/enables the
  `pg_stat_monitor` extension, the per-query snapshot reads `pg_stat_monitor`
  (aggregated across its rolling time buckets) and is written to
  `env/pg_stat_monitor.json`. **Breaking:** specs/presets using the old
  `pg_stat_statements` key must rename it to `pg_stat_monitor`.
- **Interactive soak report: full-run chart + operator event stamping.** The
  in-app report now renders a zoomable uPlot throughput/QPS/latency timeline
  with baseline and event markers, and operators can stamp events (failover /
  scale up / scale down / note) by arming an Annotate toggle and clicking a
  point — live or after the run, persisted to `events.jsonl`.
- **Prepare visibility, safety, and pg_stat_statements.**
  - **Tasks view** — a new page listing every job (runs, soaks, and the lifecycle
    jobs prepare/preflight/doctor) with state, who, start, **duration**, and the
    failure reason inline. prepare/preflight/doctor no longer "disappear": each
    links to its detail. `GET /api/jobs/{id}` returns the job + (for prepare) its
    **load metrics** (loaded units, wall time, DB size, MB/s, threads, start/end),
    shown as KPI cards on the task detail.
  - **prepare no longer fails/​no-ops silently.** An already-loaded dataset is a
    clear, actionable error (offers recreate); a missing database is a clear error
    (offers create). New `prepare` options (CLI flags + console):
    - **create the database if missing** (ask-first checkbox; connects via a
      maintenance DB — defaultdb/postgres — to `CREATE DATABASE`).
    - **recreate** — drop the whole database *or* just the benchmark tables, then
      reload. Destructive, so it requires typing the database name to confirm
      (enforced in the UI and server and harness). After a drop it waits for the
      DB to come back and **retries the load once on a fresh connection** to
      absorb the known post-drop flakiness.
    - `run --prepare` / `soak --prepare` stay idempotent (skip when present).
  - **pg_stat_statements**: preflight now reports it as enabled, or **tries to
    enable it** (`CREATE EXTENSION IF NOT EXISTS`) and reports success/failure —
    every preflight re-checks. (It powers the per-query stats already captured at
    end of run.)
  - **Targets**: edit a saved cluster's connection and **rotate its username /
    password** (`POST /api/targets/{id}`); the password reuses the encrypted ref.
  - Carried via a new per-job `options` column (migration 3).

- **Console parity (Jinja fully retired) + real concurrency.**
  - **Compare, Users, Settings, Audit are now SPA pages** — the last server-
    rendered pages are ported. The legacy paths (`/compare`, `/admin/users`,
    `/admin/settings`, `/audit`) redirect into the console; new JSON APIs back
    them (`GET/POST /api/users`, `POST /api/users/{u}`, `GET /api/audit`,
    `GET/POST /api/admin/settings`). Users page guards against an admin locking
    themselves out. Settings consolidates notifications + DigitalOcean + run
    concurrency with inline help.
  - **`max_concurrency` now actually parallelizes.** The worker runs up to N jobs
    at once, each in its own thread with its own SQLite connection (only the loop
    claims, so no claim race; `claim_next_job` still gates on `running_count`).
    Run-id is now parsed from the harness's own stdout (`… -> <run_dir>`), which
    is exact per job and therefore concurrency-safe. The value is configurable on
    the **Settings** page with a clear description of what it does and the
    trade-offs.

- **Bug-bash hardening** (multi-subsystem review). Fixes:
  - **Security — path traversal** in `/api/diff` (`a`/`b`) and `/compare/view`
    (`runs`): these are query params (not constrained like path params), so a
    viewer could read any `spec.yaml`/probe dirs via `../`. Now validated to a
    single safe segment inside `results/`.
  - **CSRF** added to `/logout` and `/api/notify/test` (cookie-session state
    changes that the double-submit design otherwise covers).
  - **Worker run-id mis-detection**: a `prepare` job could pick up
    `prepare_<slug>.json` as a bogus run_id; run-id detection is now gated to
    run/soak and filtered to manifest-bearing dirs. A cancel that kills the child
    by signal (rc < 0) is reported as **canceled**, not failed. `run_id` is never
    overwritten with NULL; the per-job secret is deleted in a `finally` (no orphan
    on error); doctor/preflight/prepare no longer send SMTP/Slack.
  - **Reconcile robustness**: a malformed/non-object `manifest.json` no longer
    aborts indexing of all runs at startup.
  - **Live PG sampler**: short psql timeout (8s) so a stalled sample during a
    failover can't block the run's end or leave an orphan psql.
  - **Preflight checklist**: any unexpected per-check exception now yields a
    degraded event instead of killing the stream.
  - **SSE log streaming** is incremental (byte offset) instead of re-reading the
    whole `harness.log` every second.
  - **Frontend**: `toYaml` now quotes scalars with YAML-special characters
    (labels/tickets/hosts with `:`/`#`/… no longer produce a broken spec); uPlot
    guards degenerate all-zero axes (incl. the QPS right axis); report KPI peak
    index is bounds-safe; New-run blocks starting with no target/host selected;
    history clears stale errors; the 401 redirect can't loop on `/login`.

- **Default-UI flip:** the console (SPA at `/ui`) is now the default — `/`, `/new`
  and `/runs/<id>` redirect into it (deep links keep working). The fully-ported
  pages are retired from the legacy UI; Compare and the admin pages (Users,
  Settings, Audit) remain server-rendered until ported and are reached from the
  console nav.
- **Fix — web/worker couldn't find the harness under systemd.** Services get a
  minimal PATH that excludes the venv bin, so shelling out to a bare
  `pgbench-harness` failed (`No such file or directory`) — breaking doctor,
  preflight/prepare and every run (a queued job went straight to failed after the
  ~3s poll and vanished from the panel). `deploy.sh` now sets
  `PGBENCH_HARNESS_BIN=<venv>/bin/pgbench-harness` in the env file, and
  `config.load_config()` resolves the CLI next to the running interpreter when the
  var is unset (defense in depth).

- **Phase 6 — reports (interactive view + CSV/print, offline kept):** the report
  page is now tabbed — an **Interactive** in-app view (KPI band; QPS/TPS-vs-threads
  and latency-vs-threads uPlot charts on dual axes; per-level table; a
  **Provenance** card with server version, max_connections, sysbench/psql/tpcc
  SHAs and dataset; soak shows baseline + per-event disruption metrics) reading a
  new `GET /api/runs/{id}/summary`, plus the **Classic** tab that renders the
  existing self-contained matplotlib report inline (offline-portability kept). A
  **Print / PDF** action (print stylesheet hides chrome; prints the classic report
  via the iframe) and **Export CSV** downloads (`/runs/{id}/csv?which=samples|
  timeseries|pg`). This completes the console (phases 1–7).

A ground-up **operator console** (React + Vite + TypeScript SPA, served by FastAPI
as prebuilt static assets — no Node on the droplet) is being built in reviewable
phases alongside the existing UI. The classic server-rendered pages keep working
throughout; the SPA lives under `/ui` until it reaches parity, then becomes the
default. The web tier still only enqueues, the worker still executes, `results/`
stays the source of truth, and RBAC/CSRF/audit/secret-handling are unchanged.

- **Phase 1 — scaffold:** Vite/TS build emitting into the package; control-room
  design system (dark-first, IBM Plex Sans/Mono, status-as-structure colour);
  app shell with auth bootstrap; the Runs (history) view ported. New JSON APIs
  `GET /api/me`, `GET /api/runs`, `GET /api/jobs` (the jobs API never exposes
  `spec_yaml`). Served at `/ui/*`.
- **Phase 2 — live cockpit:** a real-time run view fed by SSE — multi-series
  uPlot charts (TPS/QPS on dual axes, p99 latency, errors/s + reconnects/s),
  live progress (elapsed vs planned budget, level completion), and a first-class
  console pane (filter, follow, severity highlighting). The stream is now
  **incremental** (`hello`/`log`/`samples`-with-row-offset/`progress`/`done`) —
  no more re-sending the last 300 samples every second, and reconnect catches up
  cleanly. New `GET /api/runs/{id}`; `max_concurrency` is now settable via
  `GET/POST /api/settings[/concurrency]` (admin) so several clusters can run at
  once. Per-run actions (report/spec/artifacts/mark/resume/cancel) are a coherent
  surface on the detail page.
- **Phase 3 — targets, re-run, inline reports:**
  - **Inline reports:** a run's self-contained report renders *directly in the
    console* (same-origin iframe) for past or in-flight runs — view it without
    downloading; download / open-raw / regenerate remain.
  - **Saved targets:** a Targets page backed by the existing `targets` table —
    save a cluster's connection + password once (password encrypted via the
    secret store, never in spec/DB/logs/reports/artifacts). New run lets you
    **pick a saved target or enter a host inline** (fixes the missing-hostname
    hole); the form ↔ YAML stay in sync with a raw power-user editor.
  - **Re-run & clone:** one-click re-run reuses the saved target's password (no
    re-entry); clone opens New run pre-filled from a prior spec. Runs launched
    against a target resolve the password in the **worker** from the target, not
    a per-job secret.
  - Every run/job now shows its **target host**; history gains a per-row action
    menu (report / spec / clone / re-run). New APIs: `GET/POST/DELETE
    /api/targets`, `POST /api/runs/{id}/rerun`; runs index gains `target_host`.
- **Phase 4 — preflight / prepare / doctor as live flows:**
  - **Preflight** runs as a queued job and streams a **live checklist** — each
    check (tools, connectivity, server version, max_connections, pooler,
    pg_stat_statements, connection-ceiling probe, dataset) with pass/warn/fail
    and the verbatim server message. The harness gained `preflight --json` (one
    structured event per check) that **reuses the exact same checks** as the
    run/soak preflight — no logic duplicated, default text output unchanged.
  - **Prepare** runs as a queued job with a live console; **doctor** is a quick
    environment-health panel (version, git SHA, sysbench/psql availability) that
    never touches a database.
  - Reuses the existing queue + worker + password injection: new job kinds
    `preflight`/`prepare`/`doctor`, a job-scoped SSE at `/api/jobs/{id}/stream`
    (structured `check` events + `log`), and `POST /api/preflight|/api/prepare`,
    `GET /api/doctor`. New SPA views: a generic live Job view and a Diagnostics
    page; New-run gains Preflight / Prepare buttons.
- **Phase 7 — installer visibility & one-command install:** `deploy.sh` now prints
  the **operator-console URL (`/ui`)**, the **installed git SHA**, and whether the
  prebuilt **console bundle is present** — on both fresh-install and `--update`
  summaries — so "did my code actually land, and where's the new UI?" is
  unambiguous. The console's built assets ship in the package (no Node on the
  droplet); the sysbench pgsql-driver check already hard-fails the install. README
  documents that the console lives at `/ui` (classic UI stays at `/`).
- **Phase 5 — live PostgreSQL metrics:** a lightweight **engine-side sampler**
  runs during run/soak and records `parsed/pg_timeseries.csv` — cache-hit %
  (computed *over the interval*, so it shows cache re-warm after a storage
  reattach), active connections, WAL MB/s, and server transactions/s. It lives in
  the harness (the CLI benefits too) and runs in the worker's child, which already
  has the password injected — the web tier never gets DB credentials. Reuses the
  existing psql helpers, never raises, and writes only numbers (the password-leak
  gate covers the new file). Streamed to the cockpit as a `pg` SSE event and shown
  in a "PostgreSQL (engine-side)" chart section, clearly labelled an IOPS-proxy
  (server counters, not device metrics). Configurable via `capture.live_pg` /
  `capture.live_pg_interval_s` (default on, every 5s).

### Fix
- **SQLite "created in a thread" 500s on the live server.** FastAPI runs sync
  dependencies in a threadpool, so a per-request connection could be opened and
  closed on different worker threads; `connect()` now uses
  `check_same_thread=False` (each connection is still used sequentially within one
  request). This 500-ed `/api/runs`, `/api/jobs` and start-run on the real uvicorn
  server (the TestClient masked it). Regression test added.

## 0.8.0

### Web app: more of the deferred Part-C capabilities
- **Notifications (SMTP + Slack):** best-effort completion/failure alerts with
  run label, status, peak QPS, and a report link. Config + secrets (SMTP
  password, Slack webhook) set in an admin **Settings** page; secrets are stored
  encrypted (never in DB/logs/artifacts). A "send test notification" button.
  Delivery never blocks or fails a run.
- **Scheduling:** a run can be queued for a future UTC time ("start at"); the
  worker only claims it once due (max-concurrency guard already enforced).
- **Config templates (versioned) + spec diff:** save the current spec as a named
  template (auto-versioned), instantiate it from the New-run dropdown, and diff
  any two specs/templates (`/api/diff`, unified diff).
- **DigitalOcean provider-metrics hook:** fetch device-side metrics for a run's
  UTC window via the DO API (token stored encrypted; cluster id in Settings),
  cached per run and exposed at `/runs/<id>/provider-metrics`; degrades cleanly
  to engine-side only when unconfigured. (Verify the DO metric path against
  current DO API docs; overlay-on-static-report is still a follow-up.)
- **systemd hardening:** `UMask=0077` on both units; worker gains
  `RestrictNamespaces` / `ProtectKernelTunables` parity.

Still deferred (clean seams): multi-loadgen SSH orchestration, interactive
inline charts in the *downloadable* report, a full per-field guided form, and
cron-recurring schedules.

## 0.7.1

### Fix: web services failed to start on a real install (invalid Fernet key)
- `deploy.sh` generated `secret.key` with `openssl rand -base64 48` (standard
  base64, 48 bytes), which is not a valid Fernet key, so `pgbench-web`/
  `pgbench-worker` crashed at startup (`Fernet key must be 32 url-safe
  base64-encoded bytes`). The installer now writes a valid key
  (`openssl rand -base64 32 | tr '+/' '-_'`).
- The secret store now **self-heals**: an invalid key file is regenerated when
  no secrets are encrypted yet, and refused with a clear message (rather than
  silently orphaning data) when secrets already exist. Recover an affected
  install by deleting `secret.key` and restarting the services.

## 0.7.0

### Self-hosted web application (new `pgbench_webapp` package)
Browser UI + JSON API over the harness, installed/run entirely on a droplet over
HTTPS. The CLI is unchanged and remains a first-class entry point; the web tier
**calls the harness library** (validator, dry-run, report/compare, mark) rather
than duplicating logic.
- **Stack:** FastAPI + Uvicorn (TLS), SQLite control-plane (index/queue/audit;
  the filesystem `results/` stays the source of truth), server-rendered UI with
  dependency-free inline JS (live canvas chart + SSE) so downloaded reports stay
  offline-viewable.
- **Execution model:** DB-backed job queue + a **separate worker** (systemd), so
  a run survives web restarts/disconnects; the worker shells out to the CLI and
  reuses `--resume`. Cancel = graceful SIGTERM.
- **Lifecycle from the UI:** guided + raw-YAML config (same validator), import/
  export YAML, dry-run preview, start run/soak, one-click **mark** (failover/
  scale, same read-time UTC clock), cancel, resume; **live progress** via SSE
  (log tail + per-second chart) that catches up on reconnect.
- **Reports & history:** on-demand report generation (view inline + download),
  filterable/sortable run history backed by a filesystem reconcile (CLI-created
  runs appear too), compare 2+ runs, artifact tarball download.
- **Auth/RBAC:** bcrypt-hashed users; roles admin/operator/viewer enforced at
  every route; HTTP Basic (API) + secure session cookies (browser); CSRF on
  state-changing cookie requests; rate-limited login; HSTS + CSP security headers.
- **Secrets:** DB password (and future DO/SMTP/Slack/SSH creds) live Fernet-
  encrypted in `secrets.enc` (0600) keyed by `secret.key`; the DB stores only a
  reference; injected to the child env at exec time. The leak gate is **extended**
  to assert no secret lands in `results/`, the DB, logs, audit, or API responses.
- **Audit log:** append-only record of every state-changing action (login, run
  start/cancel/resume, mark, user changes), viewable + CSV export (admin).
- **Installer:** `deploy.sh` (idempotent install/update/uninstall, self-signed
  SAN cert + fingerprint, systemd units, migrations, admin bootstrap, DO firewall
  guidance); see `OPERATIONS.md`.

### New (optional, backward-compatible) run-spec fields
- `run.tags` (list), `run.environment`, `run.ticket`, `run.owner` — for history
  filtering and grouping. Existing specs keep working unchanged.

## 0.6.0

### Soak/resilience bug fixes (from a full audit)
- **Recovery near the window end / closely-spaced events no longer false-negatives.**
  TTR/full-recovery clamped the sustain check to the seconds actually available, so a
  healthy tail shorter than `recovery_hold_s` (or an event near the end) is no longer
  reported as "never recovered".
- **Degenerate baseline is detected, not silently zero.** If the baseline window is too
  early/falls in a gap (e.g. an event in the first seconds), recovery / latency-spike /
  missed-vs-baseline are reported as "n/a" with a clear warning, instead of garbage or 0.
  The auto baseline window is now strictly *before* the first event.
- **`loadgen_restart` supervisor markers are no longer treated as user events** (no bogus
  per-event metrics/zoom); they render as faint markers on the overview only.
- Supervisor: short-segment hot-loop guard; relaunch cap counts relaunches (not segments);
  coverage no longer exceeds 100%.
- Report: event labels positioned correctly; instant-recovery (0s) handled; per-event zoom
  shows gaps as line breaks (distinct from a present 0 TPS); `report --run-dir` re-analyzes
  from raw logs so events `mark`ed after a run are picked up.

### New
- **`validate`** — lint a spec without connecting (CI-friendly).
- **`doctor`** — print version, git SHA/remote and sysbench/psql availability.
- **`run --prepare` / `soak --prepare`** — load the dataset first if missing (prepare-then-run
  in one command; replaces the overnight watcher script).
- **Graceful stop for soak** — SIGINT/SIGTERM finalizes a partial resilience report from the
  captured logs instead of leaving the run stuck.

## 0.5.0

### Resilience / soak mode (Phase 1) — failover & scaling measurement
- New `soak` run mode: fixed concurrency for a long window, retaining the full
  per-second timeline keyed on **absolute read-time UTC**. New `soak` subcommand
  and a `mark` subcommand to stamp timeline events on the same clock.
- **Load generator survives the outage:** a supervisor relaunches sysbench if it
  exits early, so a failover/scale event can never truncate the test; gaps are
  measured as downtime. (sysbench has no pgsql `--ignore-errors`, so this is the
  guarantee, not a flag.)
- Per-event disruption metrics vs a pre-event baseline (median): **hard
  downtime, time-to-first-success, error window, reconnects, TTR (95% sustained),
  full re-warm/cache-cold tail (100% sustained), peak p99 & seconds above
  N×baseline, sysbench failures, and transactions-missed-vs-baseline.** Every
  definition is documented in the report's methodology section.
- New **Resilience report** (`soak_report.html`): whole-run overview decimated to
  stay legible for multi-hour (e.g. 8h) runs — per-bucket minimum shaded so brief
  outages aren't averaged away — plus a full-resolution zoom and a plain-language
  verdict per event; tables are per-event. `report --run-dir` is mode-aware.
- Artifacts: `parsed/soak_timeseries.csv`, `parsed/soak_summary.json`
  (overlay-ready), `events.jsonl`, `raw/soak_seg*.log`. Spec gains `soak:` /
  `events:` sections and `report` resilience knobs; `soak` and `sweep` are
  mutually exclusive. Phase-2 (provider-API event automation, multi-run overlay)
  left as clean seams.

## 0.4.1

### Critical parser fix — empty reports for sysbench-tpcc
- The per-second interval parser required a colon after every field label, but
  sysbench-tpcc prints `err/s 0.00` (no colon) while keeping colons elsewhere.
  Result: **no interval lines matched**, so `samples.csv` was header-only and
  every aggregate was null — the report showed all levels as "FAILED" despite
  exit code 0. Colons after all labels are now optional. Re-run
  `pgbench-harness report --run-dir <dir>` to recover affected runs from their
  intact raw logs (no benchmark re-run needed).
- Report: a level that finishes cleanly but yields no parseable samples now
  renders as "ran (exit 0) but no per-second samples were parsed", not
  "FAILED" (which contradicted the Errors section).

## 0.4.0

### Engine-side I/O capture (IOPS proxy)
- New `capture.io_stats` (default true). Per thread level, the harness snapshots
  `pg_stat_io` (PG16+), `pg_stat_database` and `pg_stat_wal` before and after the
  run (alongside the existing bgwriter snapshots) and stores them in
  `raw/<level>_iostats.json`.
- `parsed/summary.json` gains a per-level `io` block with **read ops/s, write
  ops/s, fsync/s, extend ops/s, MB read, MB written, WAL MB(/s), WAL records/s,
  and buffer cache-hit %**, derived as deltas over the level (8 KB blocks).
- The report gets a **"Storage I/O (engine-side)"** section (table + read-vs-write
  ops chart); `compare` gets write-ops/s and read-ops/s overlays.
- These are *logical* I/O as PostgreSQL issued it — an IOPS proxy for managed
  clusters where device metrics aren't reachable, not device IOPS. Each source
  degrades to "n/a" gracefully on older servers.

## 0.3.0

### Bug fixes (from a full code audit)
- **Report could not be scrolled.** `body { overflow: hidden }` propagated to
  the viewport and disabled page scrolling; it also clipped wide tables, which
  looked like "values not populating". Removed it; the banner now rounds its
  own corners. All tables are wrapped in horizontally-scrollable containers
  with sticky headers and proper `<thead>/<tbody>` (correct zebra striping).
- **Secret could reach files under `results/`.** `atomic_write_text` now
  redacts the registered password by default, so bgwriter / pg_stat_statements
  snapshots, the pg_settings dump, env files and the report HTML are all safe
  even if a libpq error echoed connection parameters.
- **Connection-ceiling probe could report a false OK.** It now waits the full
  grace period (a single fast refusal no longer short-circuits it), counts a
  holder as established only if it is still alive at the deadline, and drains
  all stderr pipes (no leaked-pipe / deadlock risk).
- **Charts crashed on zero-throughput levels.** `set_ylim(0, 0)` is avoided
  when a level ran but did no work.
- **Wall time was wrong after `--resume`.** It is now the sum of per-level
  durations, not finished-minus-created (which included the idle gap before a
  resume).
- **Resume picked the wrong directory** when same-second runs got a `-N`
  suffix; selection is now by manifest mtime.

### Comparison module overhaul
- Per-run KPI band and a "winner" callout (highest peak QPS + margin).
- New charts: TPS overlay, **latency-vs-throughput efficiency** frontier, and
  **QPS relative to a baseline** run.
- Side-by-side table with a coloured Δ column (2-run compares).
- Settings diff lists **key settings first** (bold), then the rest.
- Duplicate run labels are disambiguated; larger, safer colour palette.

### Report enhancements
- Overview now shows server `max_connections`, dataset size, and `search_path`.
- Print-friendly stylesheet; KPI cards; table-of-contents.

## 0.2.0
- Dataset conformance checks (presence + size vs spec), search_path-aware
  resolution, prepare-phase load metrics, enterprise report redesign.

## 0.1.0
- Initial harness: preflight / prepare / run / report / compare / list,
  sysbench parser, self-contained HTML reports.
