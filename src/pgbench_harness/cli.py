"""Command-line interface for pgbench-harness."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from pgbench_harness import __version__
from pgbench_harness.errors import HarnessError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pgbench-harness",
        description="Repeatable PostgreSQL benchmarking harness (sysbench) with HTML reports.",
    )
    p.add_argument("--version", action="version", version=f"pgbench-harness {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("preflight", help="connectivity, version and limits checks")
    pf.add_argument("--spec", required=True, type=Path, help="run spec YAML")

    pr = sub.add_parser("prepare", help="load the dataset (idempotent)")
    pr.add_argument("--spec", required=True, type=Path)
    pr.add_argument("--results-dir", type=Path, default=Path("results"),
                    help="where prepare logs/load-metrics are stored (default: results/)")

    va = sub.add_parser("validate", help="validate a spec without connecting (CI-friendly)")
    va.add_argument("--spec", required=True, type=Path)

    dr = sub.add_parser("doctor", help="show version, git SHA/remote and tool availability")

    rn = sub.add_parser("run", help="execute the full sweep(s) and generate the report")
    rn.add_argument("--spec", required=True, type=Path)
    rn.add_argument("--results-dir", type=Path, default=Path("results"))
    rn.add_argument("--resume", action="store_true",
                    help="resume the latest run for this label, skipping completed levels")
    rn.add_argument("--run-dir", type=Path, default=None,
                    help="explicit run directory to resume (with --resume)")
    rn.add_argument("--prepare", action="store_true",
                    help="load the dataset first if missing (prepare-then-run in one command)")
    rn.add_argument("--dry-run", action="store_true",
                    help="print the sysbench command per level and the wall-clock budget, then exit")

    sk = sub.add_parser("soak", help="fixed-concurrency resilience run (failover/scale) + report")
    sk.add_argument("--spec", required=True, type=Path)
    sk.add_argument("--results-dir", type=Path, default=Path("results"))
    sk.add_argument("--prepare", action="store_true",
                    help="load the dataset first if missing (prepare-then-soak in one command)")
    sk.add_argument("--dry-run", action="store_true",
                    help="print the soak sysbench command and planned events, then exit")

    mk = sub.add_parser("mark", help="stamp a timeline event into a (running) soak run")
    mk.add_argument("--run-dir", required=True, type=Path)
    mk.add_argument("--type", required=True,
                    choices=["failover", "scale_up", "scale_down", "note"],
                    help="event type")
    mk.add_argument("--label", default="", help="short label shown on the chart/table")
    mk.add_argument("--note", default="", help="free-text note")

    rp = sub.add_parser("report", help="(re)generate the HTML report for a run")
    rp.add_argument("--run-dir", required=True, type=Path)

    cp = sub.add_parser("compare", help="comparison report across runs")
    cp.add_argument("--runs", required=True, nargs="+",
                    help="run ids (under --results-dir) or paths to run directories")
    cp.add_argument("--results-dir", type=Path, default=Path("results"))
    cp.add_argument("--out", required=True, type=Path)

    ls = sub.add_parser("list", help="tabulate all stored runs")
    ls.add_argument("--results-dir", type=Path, default=Path("results"))
    return p


def _resolve_run_dir(token: str, results_dir: Path) -> Path:
    cand = Path(token)
    if (cand / "manifest.json").exists():
        return cand
    cand = results_dir / token
    if (cand / "manifest.json").exists():
        return cand
    raise HarnessError(
        f"run '{token}' not found (looked for {token}/manifest.json and "
        f"{results_dir / token}/manifest.json)",
        hint="pass a run id under --results-dir or a path to a run directory.",
    )


def _cmd_compare(args: argparse.Namespace) -> int:
    from pgbench_harness.compare import generate_compare

    dirs = [_resolve_run_dir(t, args.results_dir) for t in args.runs]
    out = generate_compare(dirs, args.out)
    print(f"comparison report written: {out}")
    return 0


def _peak_qps(run_dir: Path) -> str:
    """Best mean QPS across levels, read from the summary contract (best effort)."""
    import json

    path = run_dir / "parsed" / "summary.json"
    try:
        levels = json.loads(path.read_text(encoding="utf-8"))["levels"]
        vals = [l["qps_avg"] for l in levels if l.get("qps_avg") is not None]
        return f"{max(vals):,.0f}" if vals else "—"
    except (OSError, ValueError, KeyError):
        return "—"


def _cmd_list(args: argparse.Namespace) -> int:
    from pgbench_harness.manifest import Manifest

    rows = []
    if args.results_dir.exists():
        for d in sorted(args.results_dir.iterdir()):
            if (d / "manifest.json").exists():
                m = Manifest.load(d)
                ok = sum(1 for l in m.levels if l.status == "ok")
                rows.append((m.run_id, m.label, m.edition, m.tshirt_size, m.status,
                             m.created_utc, f"{ok}/{len(m.levels)}", _peak_qps(d)))
    if not rows:
        print(f"no runs found under {args.results_dir}")
        return 0
    headers = ("run_id", "label", "edition", "size", "status", "created_utc",
               "levels_ok", "peak_qps")
    widths = [max(len(headers[i]), *(len(str(r[i])) for r in rows)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*map(str, r)))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            from pgbench_harness.runner import cmd_preflight
            return cmd_preflight(args.spec)
        if args.command == "prepare":
            from pgbench_harness.runner import cmd_prepare
            return cmd_prepare(args.spec, args.results_dir)
        if args.command == "validate":
            from pgbench_harness.runner import cmd_validate
            return cmd_validate(args.spec)
        if args.command == "doctor":
            from pgbench_harness.runner import cmd_doctor
            return cmd_doctor()
        if args.command == "run":
            from pgbench_harness.runner import cmd_run
            return cmd_run(args.spec, args.results_dir, resume=args.resume,
                           run_dir_opt=args.run_dir, dry_run=args.dry_run,
                           prepare=args.prepare)
        if args.command == "soak":
            from pgbench_harness.runner import cmd_soak
            return cmd_soak(args.spec, args.results_dir, dry_run=args.dry_run,
                            prepare=args.prepare)
        if args.command == "mark":
            from pgbench_harness.runner import cmd_mark
            return cmd_mark(args.run_dir, args.type, args.label, args.note)
        if args.command == "report":
            from pgbench_harness.runner import cmd_report
            return cmd_report(args.run_dir)
        if args.command == "compare":
            return _cmd_compare(args)
        if args.command == "list":
            return _cmd_list(args)
        raise AssertionError(f"unhandled command {args.command}")
    except HarnessError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint:  {exc.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — partial results (if any) are resumable with `run --resume`.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
