# Changelog

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
