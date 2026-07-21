# pg-bench-harness

`pgbench-harness` is a repeatable PostgreSQL benchmarking harness for
DigitalOcean's managed PostgreSQL editions (Standard and Advanced). A single
YAML spec fully defines a run; the harness drives **sysbench** (including
[sysbench-tpcc](https://github.com/Percona-Lab/sysbench-tpcc)), captures the
database/server environment, and produces a **self-contained HTML report**
per run plus cross-run comparison reports.

```
pgbench-harness validate    --spec run.yaml        # lint a spec without connecting (CI-friendly)
pgbench-harness doctor                             # version, git SHA/remote, sysbench/psql availability
pgbench-harness preflight   --spec run.yaml        # connectivity, version, limits checks
pgbench-harness prepare     --spec run.yaml        # load the dataset (idempotent, records load metrics)
pgbench-harness run         --spec run.yaml [--prepare]   # steady-state thread sweep + report
pgbench-harness soak        --spec soak.yaml [--prepare]  # resilience: fixed load through a failover/scale event
pgbench-harness mark        --run-dir results/<run_id> --type failover --label "..."  # stamp an event on a soak
pgbench-harness report      --run-dir results/<run_id>/   # regenerate the HTML report (sweep or soak)
pgbench-harness compare     --runs <run_id> <run_id> --out compare.html
pgbench-harness list        [--results-dir results/]
```

`--prepare` on `run`/`soak` loads the dataset first if it's missing (prepare-then-run in
one command). A long `soak` can be stopped with Ctrl-C (or `kill`/SIGTERM) and still
finalizes a partial resilience report.

There are two run modes, chosen by the spec: a **`sweep`** section → steady-state
thread sweep (`run`); a **`soak`** section → fixed-concurrency resilience run
(`soak`). They are mutually exclusive. `run` re-runs preflight automatically and
`report` exists separately so reports can be regenerated after template or
parser improvements **without re-running benchmarks** (raw sysbench logs are
the source of truth; `parsed/` is rebuilt from them every time).

---

## Install on a fresh Ubuntu 24.04 Droplet

```bash
# 1. System packages: psql client, sysbench (Ubuntu's sysbench includes the
#    PostgreSQL (pgsql) driver), git, Python tooling
sudo apt-get update
sudo apt-get install -y postgresql-client sysbench git python3-pip python3-venv

# Verify the pgsql driver is present (must not error):
sysbench oltp_read_only --db-driver=pgsql help >/dev/null && echo "pgsql driver OK"

# 2. sysbench-tpcc Lua scripts (path goes into workload.tpcc_path)
sudo git clone https://github.com/Percona-Lab/sysbench-tpcc /opt/sysbench-tpcc

# 3. The harness itself
git clone <this-repo> pg-bench-harness && cd pg-bench-harness
python3 -m venv .venv && source .venv/bin/activate
pip install .

pgbench-harness --version          # also runnable as: python -m pgbench_harness
```

The harness is self-contained per load generator; results from multiple
machines are merged by simply copying `results/<run_id>/` directories into
one `results/` folder (see [compare](#comparing-runs)).

## 5-minute quickstart

Use [`examples/quickstart.yaml`](examples/quickstart.yaml) (threads `[1, 4]`,
`duration_s: 60`) — edit `target:` for your cluster, then:

```bash
export PGB_TARGET_PASSWORD='<the doadmin password>'   # never goes in the spec

pgbench-harness preflight --spec examples/quickstart.yaml
pgbench-harness prepare   --spec examples/quickstart.yaml
pgbench-harness run       --spec examples/quickstart.yaml --dry-run   # sanity-check commands + budget
pgbench-harness run       --spec examples/quickstart.yaml

xdg-open results/quickstart-oltp-*/report.html
```

For real sweeps (hours), run under `tmux` or `nohup` — the harness never
prompts during `run` and logs all progress to stdout **and**
`results/<run_id>/harness.log`:

```bash
nohup pgbench-harness run --spec examples/tpcc-full.yaml > run.out 2>&1 &
```

If the process is killed mid-sweep, resume with:

```bash
pgbench-harness run --spec examples/tpcc-full.yaml --resume
# or, explicitly:
pgbench-harness run --spec examples/tpcc-full.yaml --resume --run-dir results/<run_id>
```

Resume skips levels already **completed** — both `ok` and `failed` outcomes
count as completed (a recorded failure is a result; re-running it would
change the run). A level that was mid-flight when the process died
(`running`) is re-executed.

## Run specification reference

A single YAML file fully defines a run. Unknown keys and missing required
keys fail immediately with a message naming the key.

```yaml
run:
  label: "advanced-8c32g-tpcc-small"     # required; run_id = slug(label) + UTC timestamp
  edition: advanced                       # required; standard | advanced (metadata only)
  tshirt_size: 8c32g                      # required; metadata only
  notes: "baseline, operator defaults"    # optional; lands in the report header
  tags: [nightly, tpcc]                    # optional; free-form, for history filtering/grouping
  environment: prod-like                   # optional; e.g. staging / prod-like
  ticket: DBAAS-1234                       # optional; tracking reference
  owner: pat                               # optional; who owns the run

target:
  host: private-xyz.db.ondigitalocean.com # required
  port: 5432                              # required
  database: sbtest                        # required
  user: doadmin                           # required
  password_env: PGB_TARGET_PASSWORD       # required; NAME of the env var holding the
                                          # password. A literal password key is rejected.
  sslmode: require                        # optional; default "require" (passed as PGSSLMODE)

workload:
  type: tpcc                              # required; tpcc | oltp_read_only |
                                          #           oltp_read_write | oltp_write_only
  tpcc_path: /opt/sysbench-tpcc           # required when type=tpcc
  tables: 10                              # required
  scale: 30                               # required for tpcc
  # table_size: 10000                     # required for oltp_* types
  extra_args: ["--use_fk=0", "--trx_level=RC"]   # optional; passed through verbatim

sweep:
  threads: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]  # required
  duration_s: 1800                        # required; per thread level
  warmup_s: 300                           # optional (default 0); the first warmup_s of
                                          # per-second samples are DISCARDED before any
                                          # aggregate — no separate warmup process runs
  cooldown_s: 120                         # optional (default 0); sleep between levels
  repetitions: 2                          # optional (default 1); full ladder repeated N times

capture:                                  # whole section optional
  pg_settings: true                       # default true; full pg_settings CSV dump
  pg_stat_monitor: auto                   # auto (default) = capture if extension exists;
                                          # true = fail preflight if missing; false = skip
  bgwriter_stats: true                    # default true; pg_stat_bgwriter snapshot
                                          # before/after each level (raw/<level>_bgwriter.json)
  io_stats: true                          # default true; pg_stat_io / pg_stat_database /
                                          # pg_stat_wal snapshots -> per-level engine-side
                                          # I/O rates (IOPS proxy) in the report & summary
  histogram: true                         # default true; pass --histogram to sysbench

report:                                   # whole section optional
  percentiles: [50, 95, 99]               # default [50, 95, 99]
  timeseries_levels: [8, 64, 256]         # default []; thread levels that get
                                          # QPS-over-time charts (must be in sweep.threads)
  variance_warn_pct: 10                   # default 10; highlight rep-to-rep QPS delta above this
```

### Secrets policy

The password is read from `target.password_env` at exec time and handed to
`psql` and `sysbench` through the child process environment (`PGPASSWORD`;
sysbench's pgsql driver uses libpq, which falls back to `PGPASSWORD` /
`PGSSLMODE` when the corresponding options are unset). It never appears on a
command line, in logs, manifests, reports, stored spec copies, or stack
traces; a redaction filter additionally scrubs the password string from all
harness log output, and the test suite asserts no file under `results/` ever
contains it.

## Execution semantics

### Preflight

1. Records `sysbench --version`, `psql --version`, and the sysbench-tpcc
   checkout's `git rev-parse HEAD`.
2. Connects via psql: `SELECT version()`, `SHOW server_version`,
   `max_connections`, and a best-effort pooler probe (`SHOW pool_mode` —
   failing is expected against PgBouncer app databases and plain PostgreSQL;
   the raw behavior is recorded as metadata either way).
3. **Connection-ceiling probe:** opens `max(sweep.threads)` simultaneous
   connections (cheap `SELECT pg_sleep` holders, launched in order with a
   10 ms stagger). If the target refuses (e.g. PgBouncer `max_client_conn`),
   preflight aborts within seconds reporting how many connections succeeded,
   the (launch-order) index of the first refused connection, and the verbatim
   server error — instead of failing mid-sweep hours later.
4. Verifies the dataset is present **and conforms to the spec**:
   * every expected benchmark table exists *by name* (`warehouse1..N` × 9
     tpcc tables, or `sbtest1..N`) — unrelated tables can never satisfy the
     check;
   * the canary table (`warehouse1`/`sbtest1`) has the columns the workload's
     schema defines — a same-named foreign table aborts with a clear message
     rather than being benchmarked or overwritten;
   * the loaded **size matches the spec**: tpcc warehouse count must equal
     `scale` exactly; oltp `max(id)` must be within ±10% of `table_size`.
     Changing `scale: 30` → `scale: 60` against an already-loaded cluster
     therefore aborts ("found 30, spec configures 60") instead of silently
     benchmarking the wrong dataset — drop the tables (or use a fresh
     database) and `prepare` again. The harness never reloads or "tops up"
     on top of existing data;
   * non-benchmark tables in `public` produce a **warning** (shared-database
     contention risk), recorded in the manifest and shown in the report.

   If the dataset is simply absent, you are told to run `prepare`; `run`
   refuses to prepare silently. Preflight also warns when
   `max(sweep.threads)` exceeds 8× the load generator's CPU count (loadgen
   bottleneck risk).

### Prepare and load metrics

`prepare` is idempotent: if the dataset already exists *and matches the spec*
it does nothing; if it conflicts (wrong size, partial load, unrecognized
schema) it aborts with the same messages as preflight. When it does load, it
records **data-load metrics** to `results/prepare_<host>-<db>.json`: load
wall time, database size after load (`pg_database_size`), derived MB/s
throughput, rows/warehouses loaded, and the thread count used. The next `run`
against the same target+workload attaches these to `env/prepare_stats.json`
and the report shows a "Data load" card. (sysbench's prepare phase emits no
per-second metrics, so latency percentiles are not available for the load —
only aggregate throughput.)

### Run

For each repetition × thread level, in order: snapshot pre-level DB stats →
run sysbench with `--report-interval=1 --percentile=99` (plus `--histogram`)
streaming output **live** (line-buffered) to `raw/rep<r>_t<NNN>.log` →
snapshot post-level stats → sleep `cooldown_s`.

* A failed level (sysbench non-zero exit, e.g.
  `FATAL: Worker threads failed to initialize`) is recorded with its verbatim
  error lines in the manifest, and the sweep **continues**; the run is marked
  `partial`. One bad level never destroys hours of prior results.
* `manifest.json` is rewritten atomically after every level, making the run
  crash-resumable (`run --resume`).
* `run --dry-run` prints the exact sysbench command per level and the planned
  wall-clock budget (sum of durations + cooldowns), then exits.

### Parsing and warm-up

Per-second interval lines, the final summary block, and the latency histogram
are parsed from the raw logs. The first `warmup_s` of interval samples are
dropped before computing any aggregate; every aggregate in the report covers
the steady-state window only (stated in the report header).

**Latency percentile semantics:** the latency on interval lines reflects only
the value of sysbench's `--percentile` flag (always 99 here). All percentiles
in `report.percentiles` are computed from the `--histogram` latency
distribution by linear interpolation over cumulative bucket counts. With
`capture.histogram: false`, only the declared 99th percentile (from the
summary block) plus min/avg/max are available; other percentiles render as
"—".

## Results layout

```
results/<run_id>/
  manifest.json          # status, timings, per-level outcomes, failure details
  harness.log            # full harness log for the run
  spec.yaml              # verbatim copy of the input spec (password_env NAME only)
  env/
    pg_settings.csv      # full dump: name,setting,unit,source
    server_version.txt   sysbench_version.txt   tpcc_git_sha.txt
    harness_git_sha.txt  host_info.txt          spec.yaml
  raw/rep<r>_t<NNN>.log              # live-streamed sysbench output
  raw/rep<r>_t<NNN>_bgwriter.json    # pre/post pg_stat_bgwriter snapshots
  raw/rep<r>_t<NNN>_iostats.json     # pre/post pg_stat_io/database/wal snapshots
  parsed/samples.csv     # tidy per-second samples: run_id, rep, threads, t_offset,
                         # tps, qps, r, w, o, lat_p99, err_s, reconn_s
  parsed/summary.json    # per (rep, threads) steady-state aggregates — the
                         # contract `compare` consumes
  report.html            # self-contained report (charts embedded as base64 PNG)
```

## Storage I/O metrics (IOPS proxy)

With `capture.io_stats` (default on), the harness snapshots PostgreSQL's own I/O
counters — `pg_stat_io` (PG16+), `pg_stat_database`, `pg_stat_wal` — before and
after each thread level and reports the deltas as **read ops/s, write ops/s,
fsync/s, MB read/written, WAL MB/s, and buffer cache-hit %** (a "Storage I/O"
section in the report; overlays in `compare`).

These are **logical** I/O as the engine issued it (8 KB blocks), over the whole
level. On a *managed* database you have no shell on the DB host, so true
block-device IOPS isn't reachable from the harness — it lives in your provider's
monitoring (e.g. the DigitalOcean metrics/Insights graphs). Treat these numbers
as an IOPS proxy and cross-reference the provider's device graphs for the
report's UTC steady-state window. The OS page cache absorbs some reads and writes
are batched, so logical ≥ physical. On servers without `pg_stat_io`, the section
degrades gracefully (cache-hit % and WAL still come from the other views).

## Comparing runs

`parsed/summary.json` and `env/pg_settings.csv` are all `compare` needs, so
merging results from multiple load generators is just copying run
directories into one folder:

```bash
scp -r loadgen-std:results/standard-8c32g-tpcc-small-20260612T091500Z results/
scp -r loadgen-adv:results/advanced-8c32g-tpcc-small-20260612T091812Z results/

pgbench-harness compare \
  --runs standard-8c32g-tpcc-small-20260612T091500Z \
         advanced-8c32g-tpcc-small-20260612T091812Z \
  --out standard-vs-advanced-8c32g.html
```

The comparison report contains:

- a **per-run KPI band** and a "winner" callout (highest peak QPS and its margin
  over the runner-up);
- overlaid **QPS**, **TPS** and **p99-latency** vs-threads charts (one colour per
  run, legend = run label);
- a **latency-vs-throughput efficiency** chart (p99 against achieved QPS — lower
  and further right is better);
- a **QPS-relative-to-baseline** chart (every run as a % of the first run, ideal
  for "tuned vs default");
- a side-by-side headline table over the **union** of thread ladders (gaps render
  as "—"; a coloured Δ column for two-run compares);
- a **settings-diff** table showing only `pg_settings` rows that differ, with the
  curated key settings listed first.

`--runs` accepts run ids under `--results-dir` or direct paths to run
directories, and runs with duplicate labels are disambiguated automatically.

## Resilience / soak mode (failover & scaling)

`soak` mode holds a **fixed** concurrency for a long window and measures the
**client-observed disruption** when you trigger a failover or a scale event —
the empirical "how does it behave during an event" data. It's a separate mode
from the thread sweep (`soak:` and `sweep:` are mutually exclusive).

```bash
# 1. start the sustained load (runs under tmux/nohup for long windows)
pgbench-harness soak --spec examples/soak-failover.yaml

# 2. at the MOMENT you trigger the event from the provider console, stamp it:
pgbench-harness mark --run-dir results/<run_id> --type failover --label "primary failover"
```

The load generator is kept alive across the outage by a supervisor that
relaunches sysbench if it exits early (sysbench has no pgsql `--ignore-errors`),
so an event can't truncate the test; gaps are measured as downtime. Every
per-second sample carries the load generator's **read-time UTC**, and `mark`
uses the same clock, so events line up exactly with the timeline and with your
provider's own monitoring graphs.

For each event the **resilience report** (`soak_report.html`) reports, against a
pre-event baseline: hard downtime, time-to-first-success, error window,
reconnects, **TTR** (throughput back to 95% of baseline, sustained), the
**full re-warm / cache-cold tail** (back to 100% — the buffer/page-cache penalty
when storage reattaches to a new node), peak p99, sysbench failures, and
**transactions missed vs baseline** — plus a one-paragraph plain-language
verdict. The whole-run chart is decimated (with per-bucket minimums preserved)
so even an 8-hour run stays legible, with a full-resolution zoom per event.
Artifacts (`parsed/soak_timeseries.csv`, `parsed/soak_summary.json`,
`events.jsonl`) are shaped for later single-node-vs-multi-node overlay.

## Web application (self-hosted UI)

The CLI above is the engine; a self-hosted **web app** wraps it so the whole
workflow — configure, validate, dry-run, run/soak, watch live progress, mark
failover/scale events, view/download reports, browse history, compare — happens
in a browser over HTTPS, with no SSH or scp. It's installed and run entirely on
a droplet; the CLI keeps working standalone.

```bash
sudo ./deploy.sh                 # fresh install (or: sudo ./deploy.sh --update)
# prints the operator console URL (https://<public-ip>:8443/ui), the installed
# git SHA, the self-signed cert fingerprint to verify, and the DigitalOcean
# firewall rule to open the port.
```

The modern **operator console** is the default UI: visiting `/` (and `/new`,
`/runs/<id>`) redirects into the console at **`/ui`**. A few not-yet-ported admin
pages (Compare, Users, Settings, Audit) are still server-rendered and reached from
the console's nav. `deploy.sh` ships the console's prebuilt assets, so the droplet
needs no Node.js; it also prints the running git SHA so you can confirm an update
actually landed.

- **Stack:** FastAPI + Uvicorn (TLS), a SQLite control-plane (run index, job
  queue, audit) — the filesystem `results/` tree stays the source of truth — and
  a separate **worker** service so runs survive web restarts and browser
  disconnects. The worker shells out to this CLI and reuses `--resume`.
- **Auth/RBAC:** single admin at install; add `operator` (run control) and
  `viewer` (read-only) users in the UI. HTTP Basic for the API, secure session
  cookies + CSRF for the browser; rate-limited login; HSTS/CSP headers.
- **Secrets:** the DB password is captured in the UI and stored Fernet-encrypted
  (`secrets.enc`, 0600) — never in the spec, DB, logs, reports, or audit; it's
  injected into the child env at run time exactly as the CLI does.
- **Live view:** per-second TPS chart + log tail over SSE that catches up on
  reconnect; soak event markers land the instant you click "Mark failover".
- **Notifications, scheduling, templates:** opt-in SMTP/Slack alerts on run
  completion/failure (secrets encrypted, configured on the admin **Settings**
  page, with a "send test" button); queue a run for a future UTC time; save the
  current spec as a versioned **template** and diff any two specs.
- **Provider metrics (DigitalOcean):** with a DO API token (stored encrypted)
  and cluster id, the app fetches device-side metrics for a run's UTC window at
  `/runs/<id>/provider-metrics`, complementing the engine-side IOPS proxy;
  degrades cleanly to engine-side only when unconfigured.

Install, update, cert-trust, firewall, backup, RBAC, and troubleshooting are
documented in **[OPERATIONS.md](OPERATIONS.md)**. Data lives under
`/var/lib/pgbench-harness` (results + `pgbench.db` + `secret.key` + certs);
updates never touch it.

Run the web app for local development with `pip install -e '.[web]'` then
`pgbench-web` (and `pgbench-worker` in a second shell); both read `PGBENCH_*`
environment variables (see `OPERATIONS.md`).

## Development

```bash
pip install -e '.[dev]'
pytest                 # includes an end-to-end run against fake sysbench/psql
mypy src/pgbench_harness
```

The test suite includes parser fixtures with real sysbench interval/summary/
histogram output and failure logs (`FATAL: Worker threads failed to
initialize within 30 seconds!`, PgBouncer's `no more connections allowed
(max_client_conn)`), warm-up trimming, percentile interpolation, manifest
resume logic, spec validation, and the password-leak test.

## Design decisions (where the spec left room)

* **Resume semantics:** `failed` levels count as completed and are not
  retried by `--resume` — a recorded failure is a result. Start a fresh run
  (or hand-edit the manifest entry back to `pending`) to retry one.
* **Prepare parallelism:** `prepare` uses `min(16, max(sweep.threads))`
  sysbench threads.
* **Dataset check:** tpcc creates 9 tables per table-set, so the expected
  tables are `warehouse1..N` etc. (`tables × 9` names) and oltp `sbtest1..N`;
  size conformance uses `count(*)` on `warehouse1` (tiny) for tpcc and
  `max(id)` on `sbtest1` (index-only, instant) with ±10% tolerance for oltp —
  R/W workloads can drift row ids slightly, but a misconfigured 2× dataset is
  always caught. The harness assumes a dedicated database and warns when it
  detects foreign tables.
* **samples format:** CSV (not parquet) to keep dependencies minimal
  (`pyyaml`, `jinja2`, `matplotlib` only); pandas is not required.
* **Ceiling probe accounting:** holders are launched in order with a 10 ms
  stagger, so the first failed launch index closely approximates the refusing
  connection count under a pooler limit; the count of successful holders is
  reported alongside it. Probe patience is tunable via `PGB_PROBE_GRACE_S`
  (default 6 s).
* **Charts:** matplotlib (Agg) rendered to base64 PNG keeps reports fully
  offline-viewable with no JS bundle.
* **Run ids:** second-resolution UTC timestamps; a same-second collision gets
  a numeric suffix.

## Non-goals

No Prometheus/Grafana integration or OS-metric scraping (workload-side
metrics only), no provisioning/scheduling/CI, no results web server, no
engines other than PostgreSQL, no load engines other than sysbench, and no
tuning logic — the harness measures, humans tune.

## IOPS ceiling verification (evidence framework)

Settles one question with evidence: **can this cluster's pgdata volume exceed
the standard 10K block-storage IOPS limit, or not.** Three run modes feed one
evidence pipeline; every cluster-aware run (a spec with a `cluster:` section
and `$KUBECONFIG` exported) additionally captures the **storage identity**
(PVC → PV → StorageClass → placement; "config shows no high-IOPS marker" is
recorded as evidence too) and a **1s device IOPS series** from
`/proc/diskstats` inside the primary pod (node-level counters — includes WAL,
checkpoints, backups; the `pg_stat_io` logical view is captured alongside).

```bash
# 1. The full evidentiary matrix (storage-team parity): 4 sysbench OLTP
#    workloads + pgbench TPC-B/SELECT-only x thread ladder, one bundle.
pgbench-harness suite --spec examples/iops-suite.yaml --prepare

# 2. The knee finder: offered load climbs through rate steps while the
#    device series shows what the volume actually serves.
pgbench-harness soak --spec examples/iops-rate-steps.yaml

# 3. The definitive ceiling test (TEST CLUSTERS ONLY): sysbench fileio from
#    a pod pinned to the primary's node, mounting the pgdata PVC directly.
#    Iterating on threads/backlog? Set device_probe.keep_files: true to reuse
#    the prepared test files across runs (skips the multi-minute prepare);
#    a final run without it cleans them up.
pgbench-harness device-probe --spec examples/device-probe.yaml
```

All three modes are launchable from the web console: a cluster page's "IOPS
evidence" card quick-launches the suite, the knee finder, and (admin-only)
the device probe in its three patterns — mixed `rndrw`, pure-read `rndrd`,
pure-write `rndwr` — with the cluster pre-attached; New Run's
`device probe` mode exposes threads / async backlog / pattern / duration /
file geometry / keep-files as form fields.

Every mode ends with a printed **verdict** judged against the spec's
`limits:` (recorded, never hardcoded): **capped** (sustained plateau within
tolerance of the standard limit), **exceeds** (sustained above it — the
observed ceiling is reported), or **inconclusive** (the run never generated
enough pressure — the report says what stopped scaling so the run can be
redesigned). `workload.type: io_stress` sizes the dataset via `dataset_gb`
(>= 2x instance RAM defeats caches — the storage team's own report measured
cache on reads; ours must not).

The run directory is the **evidence bundle**: `report.html` (verdict, storage
identity, per-workload plain-language SQL, per-concurrency tables, scaling
charts, device timeline with reference-limit lines and event marks, honest
caveats), `evidence.json` (machine-readable everything), and CSV time series
(`parsed/device_io.csv`, `parsed/samples.csv`, `parsed/pg_timeseries.csv`).
Download it from the web UI's run page ("Artifacts") as one archive — it is
self-interpreting and can be uploaded to Claude for independent analysis with
zero additional context.

**Reading results next to PMM**: timestamps are UTC ISO everywhere and every
suite cell / rate step / fileio window is stamped as an event. Open PMM's
instances-overview scoped to the run window (specs with a `pmm:` section get
deep links in the report), find the event mark, and compare PMM's disk graphs
with `parsed/device_io.csv` — they should agree; the verdict is computed from
the harness's own series so the bundle stands alone.

## Cluster Ops (Kubernetes / Percona PG Operator)

A separate console section for operating Kubernetes-hosted PostgreSQL
clusters (PGO v2.x: Patroni HA, pgBackRest, pgBouncer) via a registered
kubeconfig — porting a field-tested bash methodology into first-class
`pgbench-harness ops` subcommands driven by the same job queue and worker:

- **Kube Targets** — kubeconfig registration (path on host or encrypted
  import), live validation checklist, read-only topology discovery
  (Patroni leader/members/TL/lag via `patronictl list -f json`, pods,
  services, backup schedules, pgBackRest repo info).
- **CR configuration** — dry-run first (exact merge patch + value diff),
  apply with a verify loop against `pg_settings` on the leader; loud
  `pending_restart` warnings ("the operator will roll pods — expect a
  failover"); pgBackRest globals verified against the rendered config in the
  pod; CR snapshots with rollback-as-a-new-patch; prep actions.
- **Backups** — full/diff/incr via direct exec or the operator's `manual:`
  Job path, from the leader or a replica (`--backup-standby`); lock
  preflight aborts instead of the rc=50 false success; 5 s samplers
  (pg_stat_archiver, archive queue depth, dual-node load); operator
  schedule pause/restore with a persistent nag; reports can overlay a live
  benchmark run's TPS with the backup window shaded.
- **Failover scenarios** — Cases A (switchover), B (pgkill), C1 (pod
  delete), C2 (node loss, experimental): capture → baseline → FIRE →
  settle → stitch → report. 5 Hz write probe through pgBouncer, per-pod log
  streams with auto-reattach, and a stitcher that classifies election vs
  restart-in-place by the authoritative Patroni leader name (never the
  probe IP), latches full-HA recovery only after an observed ready-count
  dip, and reports the pgBouncer `server_login_retry` backoff tail
  separately from DB downtime. Cross-scenario comparison table included.
- **Telemetry monitor** — continuous per-target sampler (WAL rate,
  checkpoints, archive queue, replication lag, per-member disk) that
  re-detects the leader every cycle and never blanks a whole row when one
  collector fails.
- **Parameter map** — the FULL `pg_settings` catalog introspected live from
  the leader (types, units, ranges, enum values, contexts, descriptions —
  never a hand-typed list), searchable and filterable, with typed editors
  whose validation comes from the server itself, a Patroni apply-channel
  overlay (CR-appliable vs DCS-coordinated vs Patroni-locked vs read-only),
  CR-managed provenance badges, and staged changes flowing through the
  existing dry-run → apply → verify loop.
- **Diagnostics workbench** — a click-to-run catalog of read-only checks
  covering sessions/locks, replication + slots, wraparound, cache hit, dead
  tuples/autovacuum, relation sizes, temp spill, checkpoints, WAL,
  patronictl, pgBackRest inventory, pods/events/PVC usage — results stream
  live into the ops cockpit; *live* checks support watch mode (interval
  re-sampling → moving charts). No kubectl or SQL knowledge required.
- **Health checks** — built-in intelligence: one pass evaluates
  field-standard heuristics (connection saturation, idle-in-transaction,
  inactive slots retaining WAL, wraparound distance, cache hit,
  pending_restart drift, Patroni states/lag, pod restart loops, PVC fill,
  backup staleness) into findings with severities, one-line remediations,
  and deep-links to the diagnostic that investigates each one; the worst
  severity badges the targets list.

Security invariants: kubeconfig contents and k8s-derived passwords never
touch the DB, specs, logs, SSE streams, reports, or artifacts (enforced by
the extended leak test); the web tier never runs kubectl; destructive
actions are admin-only with typed cluster-name confirmation, audited, and
mutually exclusive per target. See OPERATIONS.md §14 for the kubeconfig
flow, safety model, exact fire commands, and the smoke checklist.
