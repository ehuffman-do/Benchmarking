"""Thin data-access helpers over the SQLite control plane.

No secret value is ever stored here — only reference names into the secret store
and hashed passwords. Keep these functions small and explicit.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from pgbench_webapp.util import utc_now_iso

# ── audit ───────────────────────────────────────────────────────────

def audit(conn: sqlite3.Connection, username: Optional[str], action: str,
          target: str = "", detail: str = "") -> None:
    """Append-only audit record for every state-changing action."""
    conn.execute("INSERT INTO audit(ts_utc, username, action, target, detail) VALUES (?,?,?,?,?)",
                 (utc_now_iso(), username, action, target, detail))


def list_audit(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)))


# ── users / sessions ────────────────────────────────────────────────

def create_user(conn: sqlite3.Connection, username: str, pw_hash: str, role: str) -> None:
    conn.execute("INSERT INTO users(username, pw_hash, role, created_utc) VALUES (?,?,?,?)",
                 (username, pw_hash, role, utc_now_iso()))


def upsert_admin(conn: sqlite3.Connection, username: str, pw_hash: str) -> None:
    """Idempotent admin create/update (used by the installer's create-admin)."""
    row = get_user(conn, username)
    if row is None:
        create_user(conn, username, pw_hash, "admin")
    else:
        conn.execute("UPDATE users SET pw_hash=?, role='admin', disabled=0 WHERE username=?",
                     (pw_hash, username))


def get_user(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT id, username, role, disabled, created_utc FROM users ORDER BY username"))


def set_user_role(conn: sqlite3.Connection, username: str, role: str) -> None:
    conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))


def set_user_password(conn: sqlite3.Connection, username: str, pw_hash: str) -> None:
    conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (pw_hash, username))


def set_user_disabled(conn: sqlite3.Connection, username: str, disabled: bool) -> None:
    conn.execute("UPDATE users SET disabled=? WHERE username=?", (1 if disabled else 0, username))


def create_session(conn: sqlite3.Connection, token: str, user_id: int, expires_utc: str) -> None:
    conn.execute("INSERT INTO sessions(token, user_id, created_utc, expires_utc) VALUES (?,?,?,?)",
                 (token, user_id, utc_now_iso(), expires_utc))


def session_user(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token=? AND s.expires_utc > ? AND u.disabled=0",
        (token, utc_now_iso())).fetchone()


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# ── targets ─────────────────────────────────────────────────────────

def create_target(conn: sqlite3.Connection, name: str, host: str, port: int, dbname: str,
                  dbuser: str, sslmode: str, password_ref: str) -> int:
    cur = conn.execute(
        "INSERT INTO targets(name, host, port, dbname, dbuser, sslmode, password_ref, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, host, port, dbname, dbuser, sslmode, password_ref, utc_now_iso()))
    return int(cur.lastrowid or 0)


def get_target(conn: sqlite3.Connection, target_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()


def list_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT id, name, host, port, dbname, dbuser, sslmode FROM targets ORDER BY name"))


def get_target_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM targets WHERE name=?", (name,)).fetchone()


def delete_target(conn: sqlite3.Connection, target_id: int) -> None:
    conn.execute("DELETE FROM targets WHERE id=?", (target_id,))


def update_target(conn: sqlite3.Connection, target_id: int, **fields: Any) -> None:
    """Update connection fields (host/port/dbname/dbuser/sslmode). Never the password
    (that lives in the secret store under password_ref)."""
    allowed = {"host", "port", "dbname", "dbuser", "sslmode"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return
    clause = ",".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE targets SET {clause} WHERE id=?", (*sets.values(), target_id))


def job_for_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    """The most recent job that produced this run (for re-run target lookup)."""
    return conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY id DESC LIMIT 1",
                        (run_id,)).fetchone()


def jobs_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """ALL jobs that produced this run (resume/re-run can share a run_id), so a
    delete can clean up every job row, spec file and secret ref."""
    return list(conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY id", (run_id,)))


def delete_jobs_for_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("DELETE FROM jobs WHERE run_id=?", (run_id,))


def delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))


# ── runs index ──────────────────────────────────────────────────────

RUN_COLUMNS = ("run_id", "label", "edition", "tshirt_size", "mode", "workload_type",
               "status", "tags", "ticket", "owner", "environment", "peak_qps",
               "created_utc", "finished_utc", "source", "target_host")


def upsert_run(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = [c for c in RUN_COLUMNS if c in row]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "run_id")
    conn.execute(
        f"INSERT INTO runs({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
        [row[c] for c in cols])


def list_runs(conn: sqlite3.Connection, where: str = "", params: tuple = (),
              limit: int = 500) -> list[sqlite3.Row]:
    sql = "SELECT * FROM runs"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY created_utc DESC LIMIT ?"
    return list(conn.execute(sql, (*params, limit)))


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


# ── jobs / queue ────────────────────────────────────────────────────

def enqueue_job(conn: sqlite3.Connection, kind: str, spec_yaml: str, target_id: Optional[int],
                requested_by: str, scheduled_utc: Optional[str] = None,
                resume_run_id: Optional[str] = None, options: Optional[str] = None,
                kube_target_id: Optional[int] = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs(kind, state, spec_yaml, target_id, scheduled_utc, created_utc, "
        "requested_by, resume_run_id, options, kube_target_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, "queued", spec_yaml, target_id, scheduled_utc, utc_now_iso(),
         requested_by, resume_run_id, options, kube_target_id))
    return int(cur.lastrowid or 0)


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, states: tuple[str, ...] = ()) -> list[sqlite3.Row]:
    if states:
        q = ",".join("?" for _ in states)
        return list(conn.execute(f"SELECT * FROM jobs WHERE state IN ({q}) ORDER BY id DESC", states))
    return list(conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 500"))


def running_count(conn: sqlite3.Connection) -> int:
    """Running jobs that count against max_concurrency.

    Telemetry monitors (ops_monitor) run indefinitely by design, so they get
    their own lane: they never consume a benchmark/ops concurrency slot (a
    single monitor would otherwise wedge the queue forever). Their own cap is
    one per kube target, enforced at enqueue time.
    """
    return int(conn.execute("SELECT count(*) FROM jobs WHERE state='running' "
                            "AND kind != 'ops_monitor'").fetchone()[0])


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def claim_next_job(conn: sqlite3.Connection, max_concurrency: int) -> Optional[sqlite3.Row]:
    """Atomically claim the oldest eligible queued job, honouring concurrency.

    A job is eligible if not scheduled in the future. Uses an immediate
    transaction so two workers can't claim the same row.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if running_count(conn) >= max_concurrency:
            conn.execute("COMMIT")
            return None
        row = conn.execute(
            "SELECT * FROM jobs WHERE state='queued' "
            "AND (scheduled_utc IS NULL OR scheduled_utc <= ?) ORDER BY id LIMIT 1",
            (utc_now_iso(),)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE jobs SET state='running', started_utc=? WHERE id=?",
                     (utc_now_iso(), row["id"]))
        conn.execute("COMMIT")
        return get_job(conn, row["id"])
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


# ── templates / settings ────────────────────────────────────────────

def save_template(conn: sqlite3.Connection, name: str, spec_yaml: str, created_by: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM templates WHERE name=?",
                       (name,)).fetchone()
    version = int(row["v"])
    conn.execute(
        "INSERT INTO templates(name, version, spec_yaml, created_utc, created_by) VALUES (?,?,?,?,?)",
        (name, version, spec_yaml, utc_now_iso(), created_by))
    # Return the per-name version we just assigned — NOT cur.lastrowid (the table's
    # autoincrement rowid), which would make the API/audit report a bogus version.
    return version


def list_templates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT name, MAX(version) AS version, MAX(created_utc) AS created_utc "
        "FROM templates GROUP BY name ORDER BY name"))


def get_template(conn: sqlite3.Connection, name: str, version: Optional[int] = None) -> Optional[sqlite3.Row]:
    if version is None:
        return conn.execute("SELECT * FROM templates WHERE name=? ORDER BY version DESC LIMIT 1",
                            (name,)).fetchone()
    return conn.execute("SELECT * FROM templates WHERE name=? AND version=?",
                       (name, version)).fetchone()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ── cluster ops: kube targets ───────────────────────────────────────

KUBE_TARGET_FIELDS = ("name", "kubeconfig_path", "kubeconfig_ref", "context", "namespace",
                      "cr_kind", "cr_name", "pguser_secret", "pguser_secret_key",
                      "db_user", "db_name")


def create_kube_target(conn: sqlite3.Connection, **fields: Any) -> int:
    cols = [k for k in KUBE_TARGET_FIELDS if k in fields]
    sql = (f"INSERT INTO kube_targets({','.join(cols)}, created_utc) "
           f"VALUES ({','.join('?' for _ in cols)}, ?)")
    cur = conn.execute(sql, (*[fields[k] for k in cols], utc_now_iso()))
    return int(cur.lastrowid or 0)


def get_kube_target(conn: sqlite3.Connection, target_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM kube_targets WHERE id=?", (target_id,)).fetchone()


def get_kube_target_by_name(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM kube_targets WHERE name=?", (name,)).fetchone()


def list_kube_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM kube_targets ORDER BY name"))


def update_kube_target(conn: sqlite3.Connection, target_id: int, **fields: Any) -> None:
    # Drop None values: the columns are NOT NULL, so a caller passing {"cr_name":
    # None} would otherwise raise IntegrityError. Use the dedicated pause/clear
    # paths (which pass explicit "" / real values) to blank a field.
    fields = {k: v for k, v in fields.items()
              if v is not None or k in ("topology_json", "schedules_snapshot",
                                        "schedules_paused_utc")}
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE kube_targets SET {sets} WHERE id=?",
                 (*fields.values(), target_id))


def delete_kube_target(conn: sqlite3.Connection, target_id: int) -> None:
    conn.execute("UPDATE jobs SET kube_target_id=NULL WHERE kube_target_id=?", (target_id,))
    conn.execute("UPDATE ops_runs SET kube_target_id=NULL WHERE kube_target_id=?", (target_id,))
    conn.execute("DELETE FROM kube_targets WHERE id=?", (target_id,))


def insert_health_history(conn: sqlite3.Connection, kube_target_id: int,
                          status: str, crit: int, warn: int,
                          metrics: dict[str, Any]) -> None:
    conn.execute("INSERT INTO health_history(kube_target_id, ts_utc, status, "
                 "crit, warn, metrics) VALUES (?,?,?,?,?,?)",
                 (kube_target_id, utc_now_iso(), status, crit, warn,
                  json.dumps(metrics)))
    # Keep the history bounded: this is a cache for trends/transitions, not an
    # archive (the ops job logs remain the full record).
    conn.execute("DELETE FROM health_history WHERE kube_target_id=? AND id NOT IN "
                 "(SELECT id FROM health_history WHERE kube_target_id=? "
                 " ORDER BY id DESC LIMIT 500)", (kube_target_id, kube_target_id))


def list_health_history(conn: sqlite3.Connection, kube_target_id: int,
                        limit: int = 100) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM health_history WHERE kube_target_id=? "
        "ORDER BY id DESC LIMIT ?", (kube_target_id, limit)))


def active_ops_jobs(conn: sqlite3.Connection, kube_target_id: int,
                    kinds: tuple[str, ...] = ()) -> list[sqlite3.Row]:
    """Queued/running ops jobs on a kube target — the per-target mutex check."""
    sql = ("SELECT * FROM jobs WHERE kube_target_id=? "
           "AND state IN ('queued','running','canceling')")
    params: list[Any] = [kube_target_id]
    if kinds:
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params += list(kinds)
    return list(conn.execute(sql, params))


def enqueue_ops_job_atomic(conn: sqlite3.Connection, kind: str, spec_yaml: str,
                           requested_by: str, kube_target_id: int,
                           mutex_kinds: tuple[str, ...] = ()) -> Optional[int]:
    """Enqueue an ops job, atomically enforcing the per-target mutex.

    Wraps the active-jobs check and the insert in one immediate transaction so
    two concurrent requests can't both pass the check and enqueue competing
    destructive ops on the same cluster (the TOCTOU the plain check-then-insert
    allowed). Returns the new job id, or None if the mutex is held.
    ``mutex_kinds`` empty => no mutex (validate/discover/dry-run always enqueue).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if mutex_kinds and active_ops_jobs(conn, kube_target_id, mutex_kinds):
            conn.execute("COMMIT")
            return None
        cur = conn.execute(
            "INSERT INTO jobs(kind, state, spec_yaml, target_id, scheduled_utc, "
            "created_utc, requested_by, resume_run_id, options, kube_target_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kind, "queued", spec_yaml, None, None, utc_now_iso(), requested_by,
             None, None, kube_target_id))
        conn.execute("COMMIT")
        return int(cur.lastrowid or 0)
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise


# ── cluster ops: run index (results/ops/ stays source of truth) ─────

def upsert_ops_run(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    cols = ("op_run_id", "kind", "kube_target_id", "kube_target_name", "label",
            "params", "status", "linked_run_id", "headline", "created_utc",
            "finished_utc")
    vals = [row.get(c) for c in cols]
    updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
    conn.execute(
        f"INSERT INTO ops_runs({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
        f"ON CONFLICT(op_run_id) DO UPDATE SET {updates}", vals)


def get_ops_run(conn: sqlite3.Connection, op_run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM ops_runs WHERE op_run_id=?", (op_run_id,)).fetchone()


def list_ops_runs(conn: sqlite3.Connection, kube_target_id: Optional[int] = None,
                  limit: int = 200) -> list[sqlite3.Row]:
    if kube_target_id is not None:
        return list(conn.execute(
            "SELECT * FROM ops_runs WHERE kube_target_id=? ORDER BY created_utc DESC LIMIT ?",
            (kube_target_id, limit)))
    return list(conn.execute(
        "SELECT * FROM ops_runs ORDER BY created_utc DESC LIMIT ?", (limit,)))


def delete_ops_run(conn: sqlite3.Connection, op_run_id: str) -> None:
    conn.execute("DELETE FROM ops_runs WHERE op_run_id=?", (op_run_id,))
    conn.execute("UPDATE jobs SET run_id=NULL WHERE run_id=?", (op_run_id,))
