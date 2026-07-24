# Longevity Test Plan (DBAAS-8919 … DBAAS-8925)

Seven Jira sub-tasks, three harness runs. Five of the seven tests are passive
observations of the same long steady-state workload. We can use a single run and analyize mulitple features.

| Ticket | Test | Run | PMM default coverage |
| --- | --- | --- | --- |
| DBAAS-8919 | Shared buffers drift | **A** | Weak — cache-hit % and bgwriter counters only; buffer-pool *occupancy* needs `pg_buffercache` + custom queries |
| DBAAS-8920 | Connection pool saturation | **C** | Partial — DB-side connections vs `max_connections` yes; PgBouncer internals (`cl_waiting`, `max_client_conn` headroom) no |
| DBAAS-8921 | OS memory leak | **A** | Good — node/container memory + swap trends (Kubernetes/PGO targets only; no OS view on a managed DB) |
| DBAAS-8922 | Disk space growth | **A** | Good — filesystem usage + database size dashboards |
| DBAAS-8923 | Autovacuum pace | **A** (+ **B** for the sharp version) | Weak — per-table dead tuples / vacuum progress need the experimental vacuum dashboard or custom queries |
| DBAAS-8924 | Standby lag | **A** (+ **B** for the sharp version) | Good — replication lag is a default metric/dashboard |
| DBAAS-8925 | WAL archive storage soak | **A** (+ **B**) | Partial — WAL rate yes; archive queue depth (`.ready` backlog) and `pg_stat_archiver` failure trends no |

Where PMM is weak, the harness fills the gap: the ops **Telemetry monitor**
samples WAL rate, checkpoints, archive queue depth, replication lag, and
per-member disk; the **Diagnostics workbench / health checks** cover dead
tuples, autovacuum, and connection-saturation heuristics; `capture.io_stats`
snapshots engine-side I/O per run.

---

## Run A: week-long steady-state soak

**Spec:** [`examples/longevity-run-a.yaml`](../examples/longevity-run-a.yaml)
**Covers:** 8919, 8921, 8922, 8923 (baseline pace), 8924 (baseline lag), 8925 (steady volume)

One `oltp_read_write` soak: 4 threads, throttled to `--rate=300` txn/s
(headroom is intended: drift under sustained normal load, not peak
capacity), held for 7 days. Soak mode is used because its supervisor
relaunches sysbench through outages;
`max_relaunches` is raised to 500 since the run is scheduled for a week and many failures are possibe. 
Launch from the web console so the worker service owns the
process (survives SSH disconnects and web restarts).

~10GB data set.

## Run B: write-pressure step test (~half a day)

**Covers:** the sharp versions of 8923 / 8924 / 8925

Same dataset and target; a soak in `rate_steps` mode (the knee finder):
offered write rate climbs step by step, each step start auto-stamped as an
event. Judged per step: the write rate at which autovacuum stops keeping
pace (dead-tuple growth inflects), standby lag grows unbounded, and the
archive queue backs up. Near-zero extra setup — replace `duration_s` with
e.g. `rate_steps: [100, 200, 400, 800, 1600, 0]` + `step_duration_s: 3600`
(`0` = unthrottled).

Run **after** Run A: same spec skeleton, and A's report gives the baseline
each step is compared against.

## Run C: connection pool saturation (hours; strictly separate)

**Covers:** 8920

Sweep mode with a thread ladder climbing to and past the pooler /
`max_connections` limit. This test's goal is to exhaust connections — the
exact failure mode Runs A/B are protected against — so it must never share a
window with them. The harness's preflight connection-ceiling probe reports
how many connections succeed and the first refusal verbatim
(`max_client_conn`), and a failed thread level is recorded without killing
the rest of the ladder.

Run it **before** Run A (it is short and establishes the safe ceiling) or
after — never during.

**Unique setup:** knowledge of the pooler config (PgBouncer
`max_client_conn`, `pool_mode`); a loadgen sized for the ladder (preflight
warns above 8× loadgen CPUs); connection *churn* (vs held connections) would
additionally need sysbench reconnect flags.

---

## Other unique-setup callouts

- **8919:** if the ticket literally means buffer-pool occupancy drift,
  install `pg_buffercache` and sample it (custom PMM queries or periodic
  psql). If it means "memory counters don't degrade over a week," Run A +
  default PMM suffices. **Clarify the ticket's intent before building.**
- **8921:** only measurable with OS visibility — i.e. the PGO/Kubernetes
  cluster via PMM node metrics. Against a DO managed database the only
  option is DO provider metrics.
- **8924:** requires the cluster to actually have a standby (HA instance
  set in the CR). If the test cluster is single-node, rebuild it — and do
  so before Run A, alongside pmm-enable, since both roll pods.
- **8925:** confirm pgBackRest archiving is enabled (PGO default) and watch
  repo storage growth as well as the queue.

## Pass/fail metrics (agree before Run A starts)

One report should close six tickets with six graphs. Suggested judgments
over the 7-day window (after a 24 h warm-up exclusion):

- **8919:** cache-hit % and buffer counters stable; no monotonic drift.
- **8921:** container/node RSS slope ≈ 0; no swap growth.
- **8922:** disk growth attributable to dataset + WAL retention only;
  bloat plateaus (no unbounded `n_dead_tup` / relation size growth).
- **8923:** dead-tuple counts sawtooth (vacuum keeps pace) rather than ramp.
- **8924:** standby lag bounded and mean-reverting; no stair-step growth.
- **8925:** archive queue depth returns to ~0 between bursts; zero
  `pg_stat_archiver` failures; repo growth linear and within retention.
