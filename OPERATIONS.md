# pgbench-harness web app — Operations Runbook

This document covers running the self-hosted **pgbench-harness web app** on an
Ubuntu 24.04 droplet: install, update, TLS, firewall, backup/restore, RBAC,
logs, health checks, and troubleshooting.

The web app wraps the existing `pgbench-harness` CLI. All server lifecycle is
driven by a single script, **`deploy.sh`**, and two systemd services:

- `pgbench-web.service` — the HTTPS web UI/API (uvicorn + TLS).
- `pgbench-worker.service` — the queue worker that runs benchmark jobs
  (spawns `sysbench` / `psql` / the `pgbench-harness` CLI).

---

## 1. Layout at a glance

| Path | Purpose |
| --- | --- |
| `/opt/pgbench-harness` | App code + Python venv (`venv/`). Replaced on update. |
| `/opt/sysbench-tpcc` | Percona-Lab sysbench-tpcc Lua checkout. |
| `/var/lib/pgbench-harness` | **Data dir — source of truth, survives updates.** |
| `…/results` | Benchmark run outputs (`results/<run_id>/`). |
| `…/pgbench.db` | SQLite application database. |
| `…/secret.key` | Key used to encrypt stored secrets (mode `0600`). |
| `…/certs/cert.pem`, `…/certs/key.pem` | TLS cert + private key (key `0600`). |
| `…/INSTALLED_VERSION` | Install marker (the deployed version). |
| `/var/log/pgbench-harness/` | `install.log` (+ `web.log`/`worker.log` if file logging is enabled). |
| `/etc/pgbench-harness.env` | Environment file read by both services + the app. Rewritten by deploy.sh. |
| `/etc/pgbench-harness.secrets.env` | **Operator-managed secrets for the worker** (mode `0600`, root-only). Created once, never overwritten on update. |
| service user/group | `pgbench` (system user, no login shell), owns the data dir. |

### Environment contract (`/etc/pgbench-harness.env`)

The installer writes this file; both units load it via `EnvironmentFile=`:

```ini
PGBENCH_DATA_DIR=/var/lib/pgbench-harness
PGBENCH_DB=/var/lib/pgbench-harness/pgbench.db
PGBENCH_BIND=0.0.0.0
PGBENCH_PORT=8443
PGBENCH_TLS_CERT=/var/lib/pgbench-harness/certs/cert.pem
PGBENCH_TLS_KEY=/var/lib/pgbench-harness/certs/key.pem
```

To change the port or bind address, re-run the installer with `--port`/`--bind`
(it rewrites the env file and restarts the services). Hand-editing the file also
works, followed by `sudo systemctl restart pgbench-web pgbench-worker`.

### Worker secrets (`/etc/pgbench-harness.secrets.env`)

Credentials the worker needs at run time — today the PMM service-account
token for `ops pmm-enable` / `pmm-status` — go in this file, **not** in
`/etc/pgbench-harness.env` (that one is world-readable and rewritten on every
deploy). The installer creates it empty with mode `0600 root:root` and never
touches it again; only `pgbench-worker.service` loads it (via
`EnvironmentFile=-`, so a missing/empty file is fine). systemd reads it as
root before dropping privileges, which is why root-only permissions work even
though the service runs as `pgbench`.

```bash
sudoedit /etc/pgbench-harness.secrets.env     # uncomment/set PGB_PMM_TOKEN=glsa_…
sudo systemctl restart pgbench-worker
```

The token is environment-only by design: it never belongs in an ops spec, and
the harness never writes it to specs, logs, reports, or run artifacts
(dry-run renders it as `<token>`). If a PMM run aborts with
"`PGB_PMM_TOKEN` is not set", this file is the place to fix.

### PMM query source: pg_stat_statements is the default

`pg_stat_monitor` has a known memory-growth issue under sustained load
(field-verified 2026-07 on long TPC-C runs — it took down multi-hour tests).
The harness therefore defaults every PMM enable to
`query_source=pgstatements` / `pg_stat_statements`; pg_stat_monitor stays
selectable for short, QAN-rich sessions, and health checks raise a warning
whenever it appears in `shared_preload_libraries`.

Migrating a cluster that is already running PMM with pg_stat_monitor:

1. Re-run **Enable PMM** with `pg_stat_statements` selected — the pmm-agent
   query source is re-registered and `CREATE EXTENSION IF NOT EXISTS
   pg_stat_statements` runs on the primary (idempotent; pods roll once,
   HA-preserving).
2. In the parameter map, edit `shared_preload_libraries` to drop
   `pg_stat_monitor` (restart-required — the usual pending-restart /
   rollout-watch flow applies).
3. After the roll, `DROP EXTENSION pg_stat_monitor;` on the primary.

---

## 2. Fresh install

On a fresh (or existing) Ubuntu 24.04 droplet, from a checkout of this repo:

```bash
sudo ./deploy.sh
```

What it does:

1. Installs OS packages: `postgresql-client`, `sysbench`, `git`, `python3-venv`,
   `python3-pip`, `openssl`, `curl`, and verifies sysbench has the **pgsql**
   driver (fails with a fix hint if not).
2. Clones `sysbench-tpcc` to `/opt/sysbench-tpcc` (if missing).
3. Creates the `pgbench` system user and the directory layout above.
4. Copies the app code to `/opt/pgbench-harness`, creates the venv, and runs
   `pip install '.[web]'`.
5. Generates a **self-signed** TLS cert (SAN covers the public IP + hostname,
   825-day validity).
6. Initializes/migrates the SQLite DB.
7. Creates the admin account.
8. Installs + enables + starts both systemd services.
9. Prints the URL, the cert fingerprint, trust instructions, and the exact
   DigitalOcean firewall rule.

### Admin credentials

The admin password is taken from `PGBENCH_ADMIN_PASSWORD` if set, otherwise you
are prompted interactively:

```bash
# Non-interactive (e.g. from automation):
sudo PGBENCH_ADMIN_PASSWORD='choose-a-strong-one' ./deploy.sh --admin-user admin
```

`create-admin` is an idempotent upsert — re-running with a new password rotates
it for that user.

### Useful flags

| Flag | Effect |
| --- | --- |
| `--port <n>` | HTTPS port (default `8443`). |
| `--bind <addr>` | Bind address (default `0.0.0.0`). |
| `--admin-user <u>` | Admin username (default `admin`). |
| `--public-ip <ip>` | Force the IP used for the cert SAN + printed URL. |
| `--regen-certs` | Regenerate the TLS cert (see below). |
| `--update` | Force the update path. |
| `--uninstall [--purge]` | Remove services/code (and data with `--purge`). |
| `--help` | Full usage. |

The script is **idempotent**: re-running on an installed host auto-detects the
install marker and switches to the update path.

---

## 3. Update

**Updating mid-benchmark is safe.** The worker unit runs with
`KillMode=process`: a `deploy.sh --update` (or any worker restart) kills only
the worker process — a running benchmark is its own process group, keeps
running, and the restarted worker re-attaches to it by PID and converges the
job when it finishes. Long runs also survive browser disconnects entirely:
the load generator runs on this droplet, so closing the laptop, changing
networks, or logging in from elsewhere only affects the live view, which
reconnects and backfills automatically.


```bash
sudo ./deploy.sh --update
# or simply (auto-detected once INSTALLED_VERSION exists):
sudo ./deploy.sh
```

The update path: pulls/copies new code into `/opt/pgbench-harness`, runs
`pip install '.[web]'` into the existing venv, applies idempotent DB migrations,
and restarts both services. It then updates `INSTALLED_VERSION`.

**The update never touches** `results/`, `pgbench.db`, `secret.key`,
`/etc/pgbench-harness.secrets.env`, the TLS
certs, or admin credentials. Always [back up the data dir](#6-backup--restore)
before a major upgrade anyway.

---

## 4. Regenerating the TLS certificate

The installed cert is self-signed with a fixed validity (825 days). Regenerate
it (e.g. when it nears expiry, the public IP changes, or you rotate keys):

```bash
sudo ./deploy.sh --regen-certs
# pin a specific SAN IP:
sudo ./deploy.sh --regen-certs --public-ip 203.0.113.10
```

This overwrites `certs/cert.pem` + `certs/key.pem` and restarts the services.
Clients that pinned the old fingerprint must re-trust the new one (see below).
Without `--regen-certs`, existing certs are always preserved.

---

## 5. Trusting the self-signed certificate

Browsers and `curl` will reject the self-signed cert until you trust or pin it.
**Always verify the fingerprint** out-of-band (compare to the value `deploy.sh`
printed at install time).

Print the SHA-256 fingerprint on the server:

```bash
sudo openssl x509 -in /var/lib/pgbench-harness/certs/cert.pem -noout -fingerprint -sha256
```

### Browser

1. Open `https://<droplet-ip>:8443`.
2. On the warning page, view the certificate details and compare its SHA-256
   fingerprint to the value above.
3. If they match, accept/add the exception (Chrome: *Advanced → Proceed*;
   Firefox: *Advanced → Accept the Risk and Continue*).

### curl / scripts (pin the cert, don't disable verification)

Copy the public cert to the client and pass it explicitly:

```bash
# copy /var/lib/pgbench-harness/certs/cert.pem to the client as pgbench.pem
curl --cacert pgbench.pem https://<droplet-ip>:8443/healthz
```

Do **not** use `curl -k` / `--insecure` in production — it defeats the purpose.

### System-wide trust (Ubuntu client)

```bash
sudo cp pgbench.pem /usr/local/share/ca-certificates/pgbench.crt
sudo update-ca-certificates
```

---

## 6. DigitalOcean cloud firewall (open inbound TCP 8443)

The app listens on `8443/tcp`. Open it in the DO cloud firewall.

### doctl

Create a firewall and attach the droplet:

```bash
doctl compute firewall create \
  --name pgbench-web \
  --inbound-rules "protocol:tcp,ports:8443,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0" \
  --droplet-ids <DROPLET_ID>
```

Add the rule to an existing firewall:

```bash
doctl compute firewall add-rules <FIREWALL_ID> \
  --inbound-rules "protocol:tcp,ports:8443,address:0.0.0.0/0"
```

### Console

**Networking → Firewalls → (your firewall) → Inbound Rules → New rule**:
Custom TCP, Port `8443`, Sources: All IPv4 / All IPv6.

> Production tip: restrict **Sources** to your office/VPN CIDR instead of
> `0.0.0.0/0`. If you changed the port with `--port`, open that port instead.

---

## 7. Backup & restore

Everything that matters lives in **`/var/lib/pgbench-harness`**. Back up the
whole directory; it contains the results, the SQLite DB, the secret key, and the
TLS material.

### Why `secret.key` matters

`secret.key` is the encryption key the app uses to encrypt secrets stored in the
database (e.g. database connection passwords / credentials saved in run specs).
**If you restore `pgbench.db` without the matching `secret.key`, those encrypted
values cannot be decrypted** and must be re-entered. Treat `secret.key` like a
password: keep it `0600`, store backups securely, and never commit it.

### Backup (consistent snapshot)

Stop services briefly for a guaranteed-consistent SQLite snapshot, or use the
SQLite online backup if you must stay live.

```bash
# Option A: brief stop (recommended)
sudo systemctl stop pgbench-web pgbench-worker
sudo tar czf "pgbench-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz" \
  -C /var/lib pgbench-harness
sudo systemctl start pgbench-web pgbench-worker

# Option B: live, DB via SQLite online backup + the rest via tar
sudo sqlite3 /var/lib/pgbench-harness/pgbench.db \
  ".backup '/var/lib/pgbench-harness/pgbench.db.bak'"
sudo tar czf "pgbench-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz" \
  -C /var/lib pgbench-harness
```

Store the archive off-box. The critical members are:
`pgbench.db`, `secret.key`, `certs/`, and `results/`.

### Restore

```bash
sudo systemctl stop pgbench-web pgbench-worker
sudo tar xzf pgbench-backup-XXXX.tgz -C /var/lib
sudo chown -R pgbench:pgbench /var/lib/pgbench-harness
sudo chmod 0600 /var/lib/pgbench-harness/secret.key \
                /var/lib/pgbench-harness/certs/key.pem
sudo systemctl start pgbench-web pgbench-worker
```

If restoring onto a fresh host, run `sudo ./deploy.sh` first to install the code
and services, then restore the data dir over the top and restart.

---

## 8. RBAC roles

The web app authorizes actions by role. There are three:

| Role | Can do |
| --- | --- |
| **admin** | Everything: manage users/roles, edit connection profiles and stored secrets, start/stop/delete runs, change settings. |
| **operator** | Start, stop, and resume benchmark runs; view results and reports. Cannot manage users or rotate secrets. |
| **viewer** | Read-only: browse runs, results, and reports. Cannot start or modify anything. |

The initial **admin** is created by the installer. Additional users and role
assignments are managed in the web UI by an admin. To (re)create or rotate the
admin from the shell:

```bash
sudo -u pgbench env \
  PGBENCH_DATA_DIR=/var/lib/pgbench-harness \
  PGBENCH_DB=/var/lib/pgbench-harness/pgbench.db \
  PGBENCH_ADMIN_PASSWORD='new-strong-password' \
  /opt/pgbench-harness/venv/bin/python -m pgbench_webapp.admin create-admin --user admin
```

---

## 9. Logs (journald)

Both services log to the systemd journal. Prefer `journalctl`:

```bash
# Follow live
sudo journalctl -u pgbench-web -f
sudo journalctl -u pgbench-worker -f

# Last 200 lines
sudo journalctl -u pgbench-web -n 200 --no-pager

# Since a time / today
sudo journalctl -u pgbench-worker --since "2026-06-25 00:00:00"
sudo journalctl -u pgbench-web --since today

# Both services together, with priorities >= warning
sudo journalctl -u pgbench-web -u pgbench-worker -p warning -e
```

Install-time actions are also recorded in
`/var/log/pgbench-harness/install.log`. Per-run benchmark output (raw sysbench
logs, the HTML report, `harness.log`) lives under
`/var/lib/pgbench-harness/results/<run_id>/`.

---

## 10. Health check

The web app exposes an unauthenticated liveness endpoint:

```bash
curl --cacert pgbench.pem https://<droplet-ip>:8443/healthz
```

A `200 OK` means the web process is up and serving TLS. Use it for uptime
monitors and load-balancer checks. For a quick local check on the box:

```bash
curl --cacert /var/lib/pgbench-harness/certs/cert.pem https://127.0.0.1:8443/healthz
```

(The cert SAN includes `127.0.0.1`/`localhost`, so local checks verify cleanly.)

---

## 11. Service control quick reference

```bash
sudo systemctl status pgbench-web pgbench-worker
sudo systemctl restart pgbench-web pgbench-worker
sudo systemctl stop pgbench-web pgbench-worker
sudo systemctl start pgbench-web pgbench-worker
sudo systemctl enable pgbench-web pgbench-worker     # start on boot
```

---

## 12. Uninstall

```bash
# Stop + disable + remove services and /opt code; KEEP the data dir:
sudo ./deploy.sh --uninstall

# Also delete /var/lib/pgbench-harness (results, db, secret.key, certs):
sudo ./deploy.sh --uninstall --purge
```

Without `--purge`, the data dir, the `sysbench-tpcc` checkout, and the `pgbench`
user are left in place so you can reinstall later without data loss. To remove
the service user manually: `sudo userdel pgbench`.

---

## 13. Troubleshooting

### A service won't start

```bash
sudo systemctl status pgbench-web --no-pager
sudo journalctl -u pgbench-web -n 50 --no-pager
```

Common causes:

- **Missing/invalid env file** — confirm `/etc/pgbench-harness.env` exists and
  the paths in it are correct. Re-run `sudo ./deploy.sh --update` to rewrite it.
- **Entrypoint missing** — `/opt/pgbench-harness/venv/bin/pgbench-web` (or
  `…/pgbench-worker`) not found means the venv install failed. Reinstall:
  `sudo /opt/pgbench-harness/venv/bin/python -m pip install '/opt/pgbench-harness[web]'`.
- **Permissions** — the data/log dirs must be owned by `pgbench`. Fix with
  `sudo chown -R pgbench:pgbench /var/lib/pgbench-harness /var/log/pgbench-harness`.
- **ProtectSystem/ReadWritePaths** — if you moved the data dir, the unit's
  `ReadWritePaths` must include it, or writes fail under `ProtectSystem=strict`.

### TLS / certificate errors in the browser or curl

- **"Self-signed" / "not trusted"** — expected. Verify the fingerprint and
  trust/pin the cert (section 5).
- **"Cert doesn't match host / IP"** — you're connecting via a name/IP not in
  the SAN (e.g. the droplet got a new IP). Regenerate with the right SAN:
  `sudo ./deploy.sh --regen-certs --public-ip <new-ip>`.
- **Expired** — regenerate: `sudo ./deploy.sh --regen-certs`.
- **`curl` still fails after trust** — make sure you pass `--cacert` pointing at
  the *server's* `cert.pem`, not a stale copy.

### Port blocked / can't reach the URL

1. Confirm the service is listening on the box:
   `sudo ss -ltnp | grep :8443`
2. Confirm the **DO cloud firewall** allows inbound `8443/tcp` (section 6).
   This is the most common cause — DO firewalls deny by default.
3. Confirm any host firewall (`ufw`) isn't blocking it:
   `sudo ufw status` → `sudo ufw allow 8443/tcp` if `ufw` is active.
4. Confirm you're using **https** and the correct port.

### Worker not picking up jobs

```bash
sudo systemctl status pgbench-worker --no-pager
sudo journalctl -u pgbench-worker -n 100 --no-pager
```

Check, in order:

- The worker is **running** (`active`). If it crash-loops, the journal shows why.
- **sysbench pgsql driver** is present (the installer verifies this; re-check):
  `sysbench oltp_read_only --db-driver=pgsql help >/dev/null 2>&1 && echo OK`.
- **`/opt/sysbench-tpcc`** exists and is readable by `pgbench`.
- The worker and web both point at the **same** `PGBENCH_DB` (they share the
  queue via the SQLite db). Mismatched env files cause "jobs queued but never
  run" — both load `/etc/pgbench-harness.env`.
- **DB connectivity to the target Postgres** — the worker runs `psql`/`sysbench`
  against the database under test; a wrong host/credential or a closed path to
  that database makes jobs fail immediately. Inspect the run's
  `results/<run_id>/harness.log`.
- After fixing, `sudo systemctl restart pgbench-worker`.

### How to resume an interrupted run

A long benchmark (`run`/`soak`) is crash-resumable: if the process is killed
mid-sweep (host reboot, OOM, worker restart), the partial run directory under
`results/<run_id>/` is preserved and can be resumed. Resume **skips levels
already completed** (both `ok` and `failed` outcomes) and continues from where it
stopped.

From the web UI: open the interrupted run and use **Resume** (operator/admin).

From the shell (as the service user, against the same data dir):

```bash
sudo -u pgbench env \
  PGBENCH_DATA_DIR=/var/lib/pgbench-harness \
  /opt/pgbench-harness/venv/bin/pgbench-harness run \
    --spec /var/lib/pgbench-harness/results/<run_id>/run.yaml \
    --resume --run-dir /var/lib/pgbench-harness/results/<run_id>
```

Then regenerate the HTML report if needed:

```bash
sudo -u pgbench /opt/pgbench-harness/venv/bin/pgbench-harness report \
  --run-dir /var/lib/pgbench-harness/results/<run_id>/
```

### Where to look first

| Symptom | First command |
| --- | --- |
| Web down | `sudo journalctl -u pgbench-web -n 50 --no-pager` |
| Jobs stuck | `sudo journalctl -u pgbench-worker -n 100 --no-pager` |
| A specific run failed | `cat /var/lib/pgbench-harness/results/<run_id>/harness.log` |
| Install problem | `cat /var/log/pgbench-harness/install.log` |
| Health | `curl --cacert <cert> https://127.0.0.1:8443/healthz` |

## 14. Cluster Ops (kubeconfig-driven operations)

The Cluster Ops module drives PostgreSQL clusters running under the **Percona
PostgreSQL Operator (PGO v2.x)** on Kubernetes: topology discovery, CR
configuration changes, pgBackRest backups with impact capture, failover
scenarios, and continuous telemetry. Everything below applies on top of the
existing security model.

### 14.1 Kubeconfig flow

1. Copy the kubeconfig onto the app host, into the **sanctioned directory**:

   ```bash
   sudo install -o pgbench -g pgbench -m 0600 my-cluster.yaml \
        /var/lib/pgbench-harness/kubeconfigs/
   ```

   This is the only path the worker can see: both systemd units run with
   `ProtectHome=true` and `ProtectSystem=strict`, so a kubeconfig left in
   `/root` or `/home` is *invisible* to the worker (validation reports this
   explicitly). Alternatively paste the kubeconfig contents in the UI — the
   copy is stored Fernet-encrypted in the secret store and decrypted to a
   0600 temp file only for the duration of each job.
2. In the console: **Cluster Ops → Register a cluster**. Validation runs as a
   worker job and reports the API server, context, namespaces, discovered CR
   name(s), and the pguser secret; discovered names pre-fill the target.
3. **Refresh topology** captures instance/pgBouncer pods, the Patroni leader
   (via `patronictl list -f json`, parsed in Python), member roles/TL/lag,
   StatefulSets, services, pgBackRest repo info, and the operator's backup
   schedules.

### 14.2 Secrets model (do not regress)

- kubectl always runs in the **worker** with `KUBECONFIG` injected into the
  child process environment — never a flag, never written into a spec or
  artifact. The web tier never runs kubectl; every action is an enqueued job.
- The DB password is read from the cluster's pguser Secret **by the ops
  runner at runtime**, registered with the output redactor immediately, and
  passed only in psql child environments.
- Kubeconfig credential values (cert data, tokens) are registered with the
  redactor too. The extended leak test (`tests/test_ops.py`) enforces all of
  this.

### 14.3 Safety model

- Destructive actions (CR patches, backups, scenario firing, schedule
  pausing) are **admin-only** and require **typing the cluster (CR) name**.
- One destructive op per target at a time (server-side mutex).
- Scenario firing and backups run a **lock preflight**: if pgBackRest reports
  `backup/expire running`, the op **aborts** instead of colliding (rc=50).
- Pausing the operator's backup schedules snapshots them first and the UI
  nags on every Cluster Ops page until they are restored.
- Every action is audited (who, when, target, parameters, outcome).

### 14.4 Scenario fire commands (exactly what the runner executes)

| Case | Trigger |
|---|---|
| A — switchover | `kubectl exec <leader> -c database -- patronictl switchover <cluster>-ha --force` |
| B — pgkill | `kubectl exec <leader> -c database -- bash -c 'kill -9 $(head -1 /pgdata/pg*/postmaster.pid)'` |
| C1 — pod delete | `kubectl delete pod <leader> --grace-period=0 --force` |
| C2 — node loss (EXPERIMENTAL) | `kubectl cordon <node>` + `kubectl delete node <node>` |

Every scenario runs: capture streams start (5 Hz write probe through
pgBouncer, per-pod Patroni/pgBouncer logs with auto-reattach, pod/event
watches, 1 s patronictl sampler) → baseline settle → **FIRE** (exact instant
stamped in `raw/fire.marker`) → settle → stitch → report. Classification
(election vs restart-in-place) is decided by the Patroni **leader name**
before vs after — never the probe's answering IP.

### 14.5 Results layout

```
results/ops/<op_run_id>/
  meta.json          # manifest: UTC ISO + epoch-ms anchors, headline KPIs
  raw/               # capture streams — the source of truth
  parsed/            # sampler CSVs (archiver, queue depth, load, monitor)
  TIMELINE.txt       # stitched human-readable timeline
  events.csv         # stitched machine-readable events
  stitched.json      # downtime decomposition + classification
  report.html        # self-contained; regenerable: pgbench-harness ops report
  cr_snapshot.yaml   # full CR before any patch (cr-apply runs)
```

`pgbench-harness ops stitch --run-dir <dir>` re-derives everything under it
from `raw/` at any time.

### 14.6 Manual smoke checklist (first live-cluster run per phase)

1. **Foundation:** register the kubeconfig from `kubeconfigs/`; validation all
   green; topology shows the correct leader and member count; check the audit
   page for the actions.
2. **CR config:** dry-run the Patroni bundle — verify the patch JSON and diff
   match expectations **before** any apply; apply; confirm values in
   `pg_settings` on the leader and no `pending_restart` surprises; roll back;
   confirm original values.
3. **Backups:** run an `incr` off the leader (direct path) with no benchmark
   load; verify the lock preflight events, a new label in `pgbackrest info`,
   and non-empty `parsed/archiver.csv`; pause schedules, verify the nag,
   restore, verify the CR.
4. **Scenarios:** Case A first (least destructive); verify probe downtime,
   flip=YES, TL bump; then Case B and confirm flip=NO restart-in-place.
   C2 only against a disposable cluster.
5. **Monitor:** start at 60 s; verify leader re-detection by doing a Case A
   switchover mid-run and watching the leader column change.
6. **Parameter map:** take a snapshot; spot-check three parameters against
   `kubectl exec <leader> -c database -- psql -c "SELECT name, setting, unit
   FROM pg_settings WHERE name IN (...)"`; stage a benign change (e.g.
   `work_mem`), dry-run, apply, and confirm the verify step reports it live.
7. **Diagnostics:** run everything once; each selected check must produce a
   CSV in the cockpit. Run `connections` with watch 1 min and see the chart
   move.
8. **Health:** run a health check on a healthy cluster (expect ✓); then leave
   an idle transaction open (`psql` + `BEGIN;`) for >10 min and re-run —
   expect the idle-in-transaction finding with its remediation.

### 14.7 Parameter map, diagnostics, health (intelligence layer)

- **Parameter map** (`ops pg-params`, console → target → *Parameter map*):
  the FULL `pg_settings` catalog is introspected from the current leader —
  names, live values, types, units, min/max, enum values, contexts, and
  descriptions come from the server, never from a hand-typed list, so the map
  is automatically correct for the running major version and its loaded
  extensions. A static overlay classifies each parameter's **apply channel**:
  - `cr` — normal: staged edits are applied via
    `spec.patroni.dynamicConfiguration.postgresql.parameters` with the
    existing dry-run → apply → verify loop;
  - `dcs-coordinated` — Patroni coordinates these cluster-wide
    (max_connections, wal_level, ...): applying works but expect
    `pending_restart` and a rolling restart;
  - `patroni-locked` — Patroni overrides these (listen_addresses, port, ...):
    the console refuses to stage them;
  - `operator-managed` — the operator owns these and reverts them on every
    reconcile (TLS/socket/log plumbing, pgBackRest archive/restore commands,
    Patroni recovery parameters): display only, with the reason shown;
  - `readonly` — compiled in (`internal` context): display only.
  The editors are typed (bool/enum selects, numeric inputs with the server's
  own ranges), so an invalid value cannot be staged, let alone applied.
- **Diagnostics** (`ops diag`, console → target → *Diagnostics*): a curated
  catalog of read-only checks (sessions/locks, replication and slots,
  wraparound, cache hit, dead tuples, sizes, temp spill, checkpoints, WAL,
  patronictl, pgBackRest inventory, pods/events/PVC usage). Results stream
  into the standard ops cockpit as CSVs (tables + charts). Checks marked
  *live* support **watch mode** (re-sample every N seconds for up to an
  hour) — pick `connections` + watch to get a live saturation chart during
  an incident. Operator-level, no typed confirmation: nothing mutates.
- **Health** (`ops health`, target page → *Run health check*): one pass over
  field-standard heuristics — connection saturation, idle-in-transaction and
  long transactions, inactive replication slots retaining WAL, wraparound
  distance, cache hit ratio, `pending_restart` drift, Patroni member states
  and lag, pod phases/restart loops, PVC fill, backup staleness. Findings
  carry a severity (`info`/`warn`/`crit`), a one-line remediation, and a
  deep-link to the diagnostic or parameter view that investigates it. The
  worst severity is cached and badged on the targets list. Thresholds are
  overridable per run via `params.thresholds`.

### 14.8 Backups from a replica (backup-standby)

A **direct exec** backup runs `pgbackrest` inside one pod and can only see
that pod's local Postgres. On a replica that is a standby in recovery —
pgBackRest exits rc=56 (`unable to find primary cluster`). The runner refuses
this combination up front, and the console steers you right:

1. Set **Source** to a replica → the trigger path locks to **operator**.
2. `backup-standby: "y"` must be present in
   `spec.backups.pgbackrest.global` — the backup form shows whether it is set
   and offers a one-click enable (a normal cr-apply with dry-run/verify).
3. The operator's repo-host Job then coordinates the backup: the **standby
   copies the data files** while the **primary only handles backup start/stop
   and WAL switching** — that is the point: near-zero I/O impact on the
   writer.
4. Requirements: at least one healthy streaming replica (check the health
   panel), and both primary and standby reachable from the repo host. If the
   standby lags heavily, backups take longer; fix lag first.
