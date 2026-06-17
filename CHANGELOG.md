# Changelog

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
