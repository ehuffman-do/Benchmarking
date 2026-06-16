# Changelog

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
