"""SQLite control-plane: connection helper + forward-only migrations.

SQLite is an *index and control plane* over the filesystem ``results/`` tree —
never a second source of truth for benchmark data. Migrations are plain SQL
applied in order and tracked in ``schema_migrations``; the installer runs
``migrate`` on every update and it is safe to re-run.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from pgbench_webapp.config import ensure_dirs, load_config

# Each migration is (version, SQL). Append new ones; never edit applied ones.
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        disabled INTEGER NOT NULL DEFAULT 0,
        created_utc TEXT NOT NULL
    );
    CREATE TABLE sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_utc TEXT NOT NULL,
        expires_utc TEXT NOT NULL
    );
    CREATE TABLE targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        host TEXT NOT NULL, port INTEGER NOT NULL, dbname TEXT NOT NULL,
        dbuser TEXT NOT NULL, sslmode TEXT NOT NULL DEFAULT 'require',
        password_ref TEXT NOT NULL,            -- key into the encrypted secret store
        created_utc TEXT NOT NULL
    );
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        label TEXT, edition TEXT, tshirt_size TEXT, mode TEXT,
        workload_type TEXT, status TEXT,
        tags TEXT, ticket TEXT, owner TEXT, environment TEXT,
        peak_qps REAL, created_utc TEXT, finished_utc TEXT, source TEXT
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                    -- run | soak
        state TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|canceled
        spec_yaml TEXT NOT NULL,               -- never contains a secret (password_env only)
        target_id INTEGER REFERENCES targets(id),
        run_id TEXT,
        resume_run_id TEXT,                    -- set => worker adds --resume --run-dir
        scheduled_utc TEXT,
        created_utc TEXT NOT NULL,
        started_utc TEXT, finished_utc TEXT,
        pid INTEGER, exit_code INTEGER, error TEXT,
        requested_by TEXT
    );
    CREATE TABLE templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        spec_yaml TEXT NOT NULL, created_utc TEXT NOT NULL, created_by TEXT,
        UNIQUE(name, version)
    );
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL, username TEXT, action TEXT NOT NULL,
        target TEXT, detail TEXT
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX idx_jobs_state ON jobs(state);
    CREATE INDEX idx_runs_created ON runs(created_utc);
    """),
    # 2: surface each run's target cluster host in the index (read from the spec
    #    at reconcile time) so every run/job is unambiguous about where it ran.
    (2, """
    ALTER TABLE runs ADD COLUMN target_host TEXT;
    """),
    # 3: per-job options (JSON) — prepare's create-db / recreate / confirm flags.
    (3, """
    ALTER TABLE jobs ADD COLUMN options TEXT;
    """),
    # 4: Cluster Ops — Kube Targets (kubeconfig by path OR encrypted secret ref;
    #    never contents), the ops-run index (results/ops/ stays source of truth),
    #    and the job->kube-target link. schedules_snapshot non-empty == operator
    #    backup schedules are PAUSED on that target (drives the UI nag banner).
    (4, """
    CREATE TABLE kube_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        kubeconfig_path TEXT NOT NULL DEFAULT '',
        kubeconfig_ref TEXT NOT NULL DEFAULT '',
        context TEXT NOT NULL DEFAULT '',
        namespace TEXT NOT NULL DEFAULT 'percona',
        cr_kind TEXT NOT NULL DEFAULT 'perconapgcluster',
        cr_name TEXT NOT NULL DEFAULT '',
        pguser_secret TEXT NOT NULL DEFAULT '',
        pguser_secret_key TEXT NOT NULL DEFAULT 'password',
        db_user TEXT NOT NULL DEFAULT 'doadmin',
        db_name TEXT NOT NULL DEFAULT 'defaultdb',
        api_server TEXT NOT NULL DEFAULT '',
        last_validated_utc TEXT,
        topology_json TEXT,
        topology_utc TEXT,
        schedules_snapshot TEXT,
        schedules_paused_utc TEXT,
        created_utc TEXT NOT NULL
    );
    CREATE TABLE ops_runs (
        op_run_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        kube_target_id INTEGER REFERENCES kube_targets(id),
        kube_target_name TEXT,
        label TEXT, params TEXT, status TEXT,
        linked_run_id TEXT, headline TEXT,
        created_utc TEXT, finished_utc TEXT
    );
    ALTER TABLE jobs ADD COLUMN kube_target_id INTEGER REFERENCES kube_targets(id);
    CREATE INDEX idx_ops_runs_created ON ops_runs(created_utc);
    """),
    # 5: record the OUTCOME of the last validation (NULL = never validated,
    #    1 = ok, 0 = failed) so the targets list can badge broken targets
    #    instead of showing only when validation last ran.
    (5, """
    ALTER TABLE kube_targets ADD COLUMN last_validation_ok INTEGER;
    """),
    # 6: cached snapshots for the parameter map (full pg_settings catalog,
    #    introspected from the leader) and the health-check findings — same
    #    caching pattern as topology_json: filesystem/cluster is the source
    #    of truth, these give the UI an instant last-known view.
    (6, """
    ALTER TABLE kube_targets ADD COLUMN params_json TEXT;
    ALTER TABLE kube_targets ADD COLUMN params_utc TEXT;
    ALTER TABLE kube_targets ADD COLUMN health_json TEXT;
    ALTER TABLE kube_targets ADD COLUMN health_utc TEXT;
    """),
    # 7: continuous intelligence — per-target auto-health interval (0 = off)
    #    and a compact history of health evaluations (status + raw metrics)
    #    for transition alerts and trend analysis (disk-fill projection).
    (7, """
    ALTER TABLE kube_targets ADD COLUMN auto_health_s INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE health_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kube_target_id INTEGER NOT NULL REFERENCES kube_targets(id),
        ts_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        crit INTEGER NOT NULL DEFAULT 0,
        warn INTEGER NOT NULL DEFAULT 0,
        metrics TEXT
    );
    CREATE INDEX idx_health_hist ON health_history(kube_target_id, ts_utc);
    """),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open SQLite with WAL + foreign keys + row access by name.

    ``check_same_thread=False``: FastAPI runs sync route dependencies in an
    anyio threadpool, so a per-request connection's setup (create) and teardown
    (close) can run on *different* worker threads. Each connection is still used
    sequentially within a single request — never shared concurrently — so
    disabling the same-thread guard is safe and avoids
    ``SQLite objects created in a thread can only be used in that same thread``.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _applied(conn: sqlite3.Connection) -> set[int]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                 "(version INTEGER PRIMARY KEY, applied_utc TEXT)")
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(db_path: Path) -> int:
    """Apply pending migrations in order; returns how many were applied."""
    from pgbench_webapp.util import utc_now_iso

    conn = connect(db_path)
    try:
        done = _applied(conn)
        n = 0
        for version, sql in sorted(MIGRATIONS):
            if version in done:
                continue
            # connect() is autocommit; executescript commits its own statements.
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(version, applied_utc) VALUES (?, ?)",
                         (version, utc_now_iso()))
            n += 1
        return n
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """`python -m pgbench_webapp.db migrate` — run by the installer on update."""
    args = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    ensure_dirs(cfg)
    if args and args[0] == "migrate":
        applied = migrate(cfg.db_path)
        print(f"migrations: applied {applied} (db={cfg.db_path})")
        return 0
    print("usage: python -m pgbench_webapp.db migrate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
