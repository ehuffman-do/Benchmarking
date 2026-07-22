# Longevity Testing — Cluster Stability Findings (pre–Run A)

**Date:** 2026-07-21 → 2026-07-22
**Tickets:** DBAAS-8919 … DBAAS-8925 (test plan: [LONGEVITY_TEST_PLAN.md](LONGEVITY_TEST_PLAN.md))
**Target:** Advanced PG cluster `ehuff-long-a-adv-pg-sfo2` (namespace `percona`), Percona operator 2.9.0,
image `perconalab/percona-distribution-postgresql-custom:18.3-1` (PostgreSQL 18.3), 3 instance pods
on dedicated DOKS nodes (4 vCPU / 16 GiB; ~3.9 CPU / 13.3 GiB allocatable each).
**Workload:** sysbench `oltp_read_write`, 16 tables × 2 M rows (~10 GB), via the pgbench-harness
soak mode (PMM 3 monitoring, server on a separate droplet).

**Summary:** every multi-day soak attempt initially failed, the primary crashed or failed over
every ~30–45 minutes at any load level. Root-causing surfaced three distinct platform issues
(one masking the next), all now fixed or mitigated. The headline result was a confirmed,
 **per-backend memory leak in `pg_stat_monitor`**. This partially answers the question about memory leaks, but
 is not part of what would run in production and is specific to testing procedures. 

> **Images** live in `img/longevity/`

Graphs:
PMM [Snapshot](https://64.227.81.160/graph/dashboard/snapshot/AvUhHmzZ2NHPY6wxKRLNl1CngWFAwhpn?orgId=0&from=2026-07-21T21:33:35.548Z&to=2026-07-22T21:33:35.548Z&timezone=browser&var-interval=$__auto&var-region=$__all&var-node_type=$__all&var-environment=$__all&var-node_name=$__all&var-service_name=$__all&var-database=$__all&var-service_type=$__all&var-cluster=$__all&var-replication_set=$__all&var-username=$__all&var-schema=$__all) covering both failed and successful tests

Harness Runs: Working on Way to group and present these

Insights: [link](https://cloud.digitalocean.com/api/v1/insights/cartographer/dashboard/script/dbaas-percona-postgres-overview.js?theme=dark&from=now-24h&to=now&idate=true&hideTimeControls=true&service=ehuff-long-a-adv-pg-sfo2&host_id=ehuff-long-a-adv-pg-sfo2&dbaas_uuid=62f3e0e7-a515-4c7a-9151-d7ced74356be&contextId=62d6a306-5803-4560-ab6e-c4e4254883db) probably only works on DO vpn 

---

## Finding 1 — Database pods ran as BestEffort: node-pressure eviction loop

**Symptom.** Under a 16-thread (later 4-thread) throttled soak, the serving primary died every
~20–30 minutes. PMM showed the write load hopping pod → pod → pod, each hand-off coinciding with
a memory sawtooth peak and CPU spike on the pod that had been primary.

![Fig 1 — PMM Cluster Health: TPS band rotating between the three instance pods; memory sawtooth
climbing to ~85–90% on whichever pod is primary, dropping at each failover.](img/longevity/fig1-rotating-failovers.png)

**Evidence.** `kubectl get events` showed the kills were **kubelet node-pressure evictions**, not
container OOMs, and that no container in the instance pod had any memory request:

```
Warning  Evicted  pod/ehuff-long-a-adv-pg-sfo2-instance1-2lsr-0
  The node was low on resource: memory. Threshold quantity: 100Mi, available: 59296Ki.
  Container pmm-client was using 136712Ki, request is 0, ...
  Container database was using 13297724Ki, request is 0, has larger consumption of memory. ...
```

The `database` container had grown to **~12.7 GiB on a node with 13.3 GiB allocatable**
(`kubectl describe nodes`: cpu 3890m, memory 13974824Ki). With `request: 0` on every container,
the pod is **BestEffort QoS — first in line for eviction** — so ordinary page-cache growth from a
10 GB dataset marched the node into its eviction threshold and the kubelet killed the primary,
over and over. A kernel-log dump from this era confirms the same picture at `/kubepods` scope
(anon 9.3 GB across ~20 sysbench backends + 4.7 GB cache, cilium/coredns protected at
`oom_score_adj -997` while all database processes sat at `999`).

**Fix.** CR patch: `spec.instances[0].resources = requests {cpu: 2, memory: 10Gi} /
limits {memory: 10Gi}`, plus `spec.pmm.resources`, plus `shared_buffers: 2560MB`. Evictions
stopped immediately (no further `Evicted` events).

**Product note.** If any production provisioning path leaves the database container without
requests/limits, node memory pressure evicts the *primary* preferentially. Worth verifying
outside this test cluster.

---

## Finding 2 — CPU saturation triggers probe-kill failover cascades

**Symptom.** After the resources fix, 16-thread runs still failed — now with the primary's
containers being killed and restarted while a pgBackRest backup job retried in a loop.

**Evidence.** Events showed exec-based liveness probes timing out under CPU starvation, then
containerd failing to exec at all, then crash-loop backoff:

```
Warning  Unhealthy  Liveness probe failed: command timed out: "pgbackrest server-ping" timed out after 1s
Warning  Unhealthy  Liveness probe errored ... failed to exec in container: ... no running task found
Warning  BackOff    Back-off restarting failed container database in pod ...instance1-2lsr-0
```

16 unthrottled threads saturate the ~4 vCPU nodes; a fork+exec inside a starved container easily
exceeds the **1-second probe timeout**, so the kubelet kills healthy-but-busy containers. Each
failover also killed the in-flight backup, whose retry added more load causing a self-reinforcing loop
(the running backup job `backup-vdh5` created new pods every ~10 min while they continued to fail).

![Fig 2 — 16-thread run after the resources fix: evictions gone, but CPU spikes on the serving
pod line up with availability dips and leadership hand-offs — the probe-kill
era.](img/longevity/fig2-probekill-16thread.png)

**Mitigation.** The soak load is throttled well below saturation (final plan: ~25% of calibrated
peak). **Product note:** a 1 s exec probe timeout means a CPU-saturating tenant causes failover
cascades rather than graceful degradation. This might need different tuning.

---

## Finding 3 (headline) — `pg_stat_monitor` leaks backend memory without bound

**Symptom.** Even at 4 threads / 100 tps — with resources fixed, no backup running, CPU ~30% —
the primary's `database` container was killed every ~30–45 min with **nothing in kubectl events**
(this is because container-scope OOM kills emit no event). PMM showed a clean memory staircase to the 10 Gi limit that was previously set (see finding 1).

![Fig 3 — PMM Cluster Health during the 4-thread run: steady ~100 tps on the primary, memory
stair-stepping to ~75% of node (= the 10 Gi container limit) before each kill.](img/longevity/fig3-memory-staircase-4thread.png)

**Evidence chain.**

1. **The kill is a container-scope kernel OOM** — `kubectl describe pod`:

   ```
   Last State:  Terminated
     Reason:    OOMKilled
     Exit Code: 137
     Started:   Tue, 21 Jul 2026 22:19:11 -0700
     Finished:  Tue, 21 Jul 2026 22:48:30 -0700   (~29 min lifetime)
   Restart Count: 4
   Limits:   memory: 10Gi
   ```

2. **Node kernel logs (dmesg) show the leak is per-backend anonymous memory, proportional to
   connection count.** Container cgroup at its 10 Gi limit with `anon ≈ 7.5 GB`:

   - 16-thread era: ~20 postgres backends, **each ~450–525 MB anon RSS**
     (`Killed process 575271 (postgres) ... anon-rss:515836kB, shmem-rss:2181124kB ...`)
   - 4-thread era: exactly **4 backends at ~1.8–1.9 GB anon each**
     (`Killed process 643604 (postgres) ... anon-rss:1897436kB ...`)

   Same total, divided by the number of sysbench connections pointing to a 
   per-connection leak (~1 KB per query at ~500 queries/s/backend). `memory.oom.group`
   kills the whole container task group, which is why PostgreSQL's own log never records a
   child death.

3. **PostgreSQL names the leaking component.**  The following shows the results of running `pg_log_backend_memory_contexts()` on the live
   sysbench backends:

   ```
   pg_stat_monitor local store: 587202560 total in 80 blocks; ... 584220216 used   (pid 79248)
   pg_stat_monitor local store: 570425344 total in 78 blocks; ... 562661824 used   (pid 79249)
   pg_stat_monitor local store: 511705088 total in 71 blocks; ... 506157672 used   (pid 79250)
   ...
   Grand total: 591896008 bytes ... (pid 79248)
   ```

   The `pg_stat_monitor local store` context is **>97% of each backend's total memory**. i.e. The
   staging buffer that should flush to the extension's fixed shared memory per bucket, instead
   accumulates forever. Every other context is healthy (CachedPlans 4–8 KB, CacheMemoryContext
   ~1.2 MB).

`pg_stat_monitor` was loaded on these pods as part of PMM QAN enablement; the leak occurs merely
by having the library in `shared_preload_libraries` so every backend pays it, whether or not QAN
reads the data.

**Fix.**
- Removed `pg_stat_monitor` from the cluster: `spec.extensions.builtin.pg_stat_monitor: false`
  + cleaned `shared_preload_libraries` + `DROP EXTENSION pg_stat_monitor`.
- Switched PMM query analytics to **`pg_stat_statements`** (supported querySource pairing; a
  small harness fix was needed en route — the operator CRD's enum is `pgstatstatements` while
  PMM tooling uses `pgstatements`, and the harness passed the wrong vocabulary into the CR).
- Upstream bug report against pg_stat_monitor on the PG 18 build to follow (version via
  `SELECT pg_stat_monitor_version();`), with the dmesg dumps, memory-context dumps, and PMM
  graphs above as evidence.

---

## Side findings (noted for follow-up, not blocking)
- **PostgreSQL server log is discarded on these pods**: `logging_collector=off`,
  `log_destination=stderr`, and the postmaster's stderr points at a pipe that nothing persists —
  no PANIC/crash forensics available on-pod. (Memory-context dumps only became readable because
  patroni relays some postgres stderr to container stdout.)
- **`shared_preload_libraries` is assembled from three layers** (operator builtin extension
  flags + `patroni.dynamicConfiguration` + operator PMM/querySource injection), which produced
  confusing duplicated lists (`pg_stat_monitor,pgaudit,pg_stat_monitor,pgaudit`) during
  diagnosis. Duplicates are functionally harmless (libraries load once) but obscure the actual
  config; the cleanest ownership is to leave the parameter to the operator layers.
- **Replication/HA behaved well throughout**: Patroni re-elected cleanly through dozens of
  forced failovers (timeline reached 26+ in one afternoon), replicas streamed with zero lag
  once stable. Note the following screenshot is for a very short period, and Paul's explicit testing
   of failovers is almost certainly a better measure of replication/ha behavior).

![Fig 4 — PMM Patroni/PostgreSQL panel: leader/replica state, Timeline 26 (≈25 forced
failovers in one afternoon), and WAL positions during the failover-heavy
period.](img/longevity/fig4-patroni-wal.png)

---

## Status & next steps

**Post-Fix Test Runs Smoothly.** With `pg_stat_statements` in place of `pg_stat_monitor`, the same
workload that previously OOM-killed the primary every ~30–45 minutes ran **~10.5 hours clean**
(2026-07-22, 4 threads / ~112 tps): uptime monotonic on all three pods (zero restarts),
availability solid, CPU ~30% steady with harmless periodic spikes, and memory **flat at ~27%**
— no staircase (Fig 6).

![Fig 6 — post-fix verification: ~10.5 h at 4 threads / ~100 tps on pg_stat_statements. Uptime
climbs uninterrupted, availability pinned at 1, memory flat ~27% — the leak's staircase is
gone.](img/longevity/fig6-post-fix-graphs.png)

Next steps:
* continue to monitor the week-long test
* investigate periodic cpu spikes. Confirm source and see if issue 2 (probe killing pod) resurfaces
* run calibration tests to find limits for values (see [LONGEVITY_TEST_PLAN.md](LONGEVITY_TEST_PLAN.md) run B)

