# pg-bench-harness

`pgbench-harness` is a repeatable PostgreSQL benchmarking harness for
DigitalOcean's managed PostgreSQL editions (Standard and Advanced). A single
YAML spec fully defines a run; the harness drives **sysbench** (including
[sysbench-tpcc](https://github.com/Percona-Lab/sysbench-tpcc)), captures the
database/server environment, and produces a **self-contained HTML report**
per run plus cross-run comparison reports.

```
pgbench-harness preflight   --spec run.yaml        # connectivity, version, limits checks
pgbench-harness prepare     --spec run.yaml        # load the dataset (idempotent, records load metrics)
pgbench-harness run         --spec run.yaml        # execute the sweep(s), capture everything, report
pgbench-harness report      --run-dir results/<run_id>/   # regenerate the HTML report
pgbench-harness compare     --runs <run_id> <run_id> --out compare.html
pgbench-harness list        [--results-dir results/]
```

`run` re-runs preflight automatically and generates the report at the end.
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
  pg_stat_statements: auto                # auto (default) = capture if extension exists;
                                          # true = fail preflight if missing; false = skip
  bgwriter_stats: true                    # default true; pg_stat_bgwriter snapshot
                                          # before/after each level (raw/<level>_bgwriter.json)
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
  parsed/samples.csv     # tidy per-second samples: run_id, rep, threads, t_offset,
                         # tps, qps, r, w, o, lat_p99, err_s, reconn_s
  parsed/summary.json    # per (rep, threads) steady-state aggregates — the
                         # contract `compare` consumes
  report.html            # self-contained report (charts embedded as base64 PNG)
```

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
