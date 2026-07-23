"""``pgbench-harness ops`` subcommand family: parser wiring + dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from pgbench_harness.errors import HarnessError
from pgbench_harness.ops.opspec import OpsSpec, parse_ops_spec


def add_ops_parser(sub: argparse._SubParsersAction) -> None:
    ops = sub.add_parser("ops", help="cluster operations (kubeconfig-driven; see docs)")
    osub = ops.add_subparsers(dest="ops_command", required=True)

    def spec_args(p: argparse.ArgumentParser, results: bool = True) -> None:
        p.add_argument("--ops-spec", required=True, type=Path,
                       help="ops job spec YAML (never contains a secret)")
        if results:
            p.add_argument("--results-dir", type=Path, default=Path("results"))

    spec_args(osub.add_parser("validate",
                              help="validate the kubeconfig + target (live checklist)"),
              results=False)
    spec_args(osub.add_parser("discover",
                              help="read-only topology snapshot (pods, leader, backups)"),
              results=False)
    ca = osub.add_parser("cr-apply", help="patch cluster CR configuration (verify loop)")
    spec_args(ca)
    bk = osub.add_parser("backup", help="run a pgBackRest backup with impact capture")
    spec_args(bk)
    sc = osub.add_parser("scenario", help="failover scenario: capture -> FIRE -> stitch")
    spec_args(sc)
    mo = osub.add_parser("monitor", help="continuous cluster telemetry sampler")
    spec_args(mo)
    lg = osub.add_parser("logs", help="follow pod/container logs into a run dir "
                                      "(the console's live log viewer backend)")
    spec_args(lg)
    spec_args(osub.add_parser("pg-params",
                              help="snapshot the full pg_settings parameter catalog"),
              results=False)
    dg = osub.add_parser("diag", help="run read-only diagnostics (live CSV results)")
    spec_args(dg)
    spec_args(osub.add_parser("health",
                              help="evaluate health heuristics -> findings JSON"),
              results=False)
    opr = osub.add_parser("operate",
                          help="day-2 operation: restart|switchover|failover|"
                               "scale|resize|schedules")
    spec_args(opr)
    for verb, hlp in (("pmm-enable", "enable PMM 3.x monitoring end to end "
                                     f"(token from $PGB_PMM_TOKEN)"),
                      ("pmm-status", "PMM validation report only, no mutations"),
                      ("pmm-disable", "restore the pre-enablement CR + delete "
                                      "the PMM secret")):
        spec_args(osub.add_parser(verb, help=hlp))
    st = osub.add_parser("stitch", help="(re)stitch a scenario run dir from raw captures")
    st.add_argument("--run-dir", required=True, type=Path)
    rp = osub.add_parser("report", help="(re)generate the report for an op run dir")
    rp.add_argument("--run-dir", required=True, type=Path)


def _load_spec(path: Path) -> OpsSpec:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessError(f"cannot read ops spec {path}: {exc}")
    return parse_ops_spec(doc)


def cmd_ops(args: argparse.Namespace) -> int:
    cmd = args.ops_command
    if cmd == "stitch":
        from pgbench_harness.ops.stitch import restitch_run_dir
        return restitch_run_dir(args.run_dir)
    if cmd == "report":
        from pgbench_harness.ops.report_ops import generate_ops_report
        out = generate_ops_report(args.run_dir)
        print(f"ops report written: {out}")
        return 0
    spec = _load_spec(args.ops_spec)
    if cmd == "validate":
        from pgbench_harness.ops.validate import run_validate
        return run_validate(spec)
    if cmd == "discover":
        from pgbench_harness.ops.discover import run_discover
        return run_discover(spec)
    if cmd == "cr-apply":
        from pgbench_harness.ops.crconfig import run_cr_apply
        return run_cr_apply(spec, args.results_dir)
    if cmd == "backup":
        from pgbench_harness.ops.backup import run_backup
        return run_backup(spec, args.results_dir)
    if cmd == "scenario":
        from pgbench_harness.ops.scenario import run_scenario
        return run_scenario(spec, args.results_dir)
    if cmd == "monitor":
        from pgbench_harness.ops.monitor import run_monitor
        return run_monitor(spec, args.results_dir)
    if cmd == "logs":
        from pgbench_harness.ops.logs import run_logs
        return run_logs(spec, args.results_dir)
    if cmd == "pg-params":
        from pgbench_harness.ops.params import run_pg_params
        return run_pg_params(spec)
    if cmd == "diag":
        from pgbench_harness.ops.diag import run_diag
        return run_diag(spec, args.results_dir)
    if cmd == "health":
        from pgbench_harness.ops.health import run_health
        return run_health(spec)
    if cmd == "operate":
        from pgbench_harness.ops.operate import run_operate
        return run_operate(spec, args.results_dir)
    if cmd == "pmm-enable":
        from pgbench_harness.ops.pmm import run_pmm_enable
        return run_pmm_enable(spec, args.results_dir)
    if cmd == "pmm-status":
        from pgbench_harness.ops.pmm import run_pmm_status
        return run_pmm_status(spec, args.results_dir)
    if cmd == "pmm-disable":
        from pgbench_harness.ops.pmm import run_pmm_disable
        return run_pmm_disable(spec, args.results_dir)
    raise AssertionError(f"unhandled ops command {cmd}")
