"""Cluster Ops: kubeconfig-driven PostgreSQL cluster operations.

This package ports a field-tested bash methodology (failover probes, backup
impact measurement, CR tuning) into the harness as first-class CLI subcommands
(``pgbench-harness ops ...``). The webapp worker shells out to these exactly
like benchmark runs, so ops runs inherit the same properties: cancel is a
process-group signal, the web tier never executes kubectl, and the
``results/ops/`` filesystem tree is the source of truth.

Security invariants (do not regress):
* kubectl is always a subprocess with ``KUBECONFIG`` taken from the process
  environment (injected by the worker at exec time) — never a flag, never a
  file path written into specs or artifacts.
* The DB password read from the cluster's pguser Secret exists only in memory
  and in psql child environments; it is registered with the process-wide
  redactor the moment it is decoded.
"""
