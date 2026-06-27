"""DigitalOcean provider-metrics fetch (device-side CPU/memory/disk).

The harness's engine-side ``pg_stat_io`` numbers are an IOPS *proxy*; true
device metrics live in DO's monitoring. This pulls them via the DO API for a
run's UTC window so they can be shown alongside the engine-side timeline,
clearly labelled provider-side. The DO API token is a secret in the encrypted
store (ref ``do:api_token``) — never logged or persisted to any artifact. With
no token/cluster configured, callers degrade gracefully to engine-side only.

The metric endpoint base is configurable (settings ``do_metrics_base``) so it can
be pointed at the exact DO monitoring path without a code change; the default
targets DO API v2 database metrics. Verify the path against current DO API docs
before relying on the numbers in a leadership report.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from pgbench_webapp import queries
from pgbench_webapp.secrets_store import SecretStore

DO_TOKEN_REF = "do:api_token"
DEFAULT_BASE = "https://api.digitalocean.com/v2/monitoring/metrics/databases"
# (metric path, DO metric name) — adjust to the current DO API as needed.
METRICS = (("cpu", "cpu"), ("memory", "memory_available"), ("disk", "disk_usage"))


def _get(url: str, token: str, timeout: int = 15) -> Optional[dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec
            return dict(json.loads(resp.read().decode()))
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None


def fetch_metrics(conn: sqlite3.Connection, store: SecretStore, cluster_id: str,
                  start_epoch: int, end_epoch: int) -> Optional[dict[str, Any]]:
    """Fetch provider metrics for [start,end]; None if unconfigured/unavailable."""
    token = store.get(DO_TOKEN_REF)
    if not token or not cluster_id:
        return None
    base = queries.get_setting(conn, "do_metrics_base", DEFAULT_BASE)
    out: dict[str, Any] = {"source": "digitalocean", "cluster_id": cluster_id,
                           "window": [start_epoch, end_epoch], "metrics": {}}
    got = False
    for path, _name in METRICS:
        q = urllib.parse.urlencode({"host_id": cluster_id, "start": start_epoch, "end": end_epoch})
        data = _get(f"{base}/{path}?{q}", token)
        if data is not None:
            out["metrics"][path] = data
            got = True
    return out if got else None


def configured(conn: sqlite3.Connection, store: SecretStore) -> bool:
    return bool(store.get(DO_TOKEN_REF) and queries.get_setting(conn, "do_cluster_id", ""))
