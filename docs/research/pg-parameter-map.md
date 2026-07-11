# PostgreSQL Parameter Map for Cluster Ops — Research Findings

Everything below was verified against primary sources: PostgreSQL doc/source SGML and `guc*.c` for REL_13..REL_17, Patroni master source (`patroni/postgresql/config.py`) and docs RST, Crunchy PGO source (local clone `pgo/` at tag **v5.8.8** + `main`), Percona operator source (local clone `ppg/` at `main`, and raw fetches), and the Percona docs source (local clone `k8spg-docs/`). Local copies of all cited files are in `/tmp/claude-0/-home-user-Benchmarking/a4df773a-6fac-5a9f-af50-bfdacbeed740/scratchpad/` (e.g. `pg_settings_17_section.sgml`, `patroni_config.py`, `patroni_conf.rst`, `k8spg-docs/docs/immutable-options.md`).

---

## 1. pg_settings catalog: columns and context semantics (PG 13–17)

**Column set is IDENTICAL across PG 13 and PG 17** (verified by diffing the `view-pg-settings` sect1 of `REL_13_STABLE/doc/src/sgml/catalogs.sgml` against `REL_17_STABLE/doc/src/sgml/system-views.sgml` — zero diff in `<structfield>` lists). All 17 columns, in catalog order:

| column | type | semantics |
|---|---|---|
| `name` | text | parameter name (case-insensitive identifier) |
| `setting` | text | current value, **in base units** (e.g. `shared_buffers` → `16384` with unit `8kB`, not `128MB`) |
| `unit` | text | implicit base unit; NULL for unitless (see §3 for the closed set) |
| `category` | text | logical group, e.g. `Resource Usage / Memory` — **localized via gettext** (see caveat below) |
| `short_desc` | text | one-line description (localized) |
| `extra_desc` | text | longer description, often NULL (localized) |
| `context` | text | required context to set (closed set of 7, below) |
| `vartype` | text | one of `bool`, `enum`, `integer`, `real`, `string` (**deliberately NOT localized**, per source comment on `config_type_names`) |
| `source` | text | source of current value (closed set, below; **not localized**) |
| `min_val` | text | minimum allowed value; NULL for non-numeric vartypes |
| `max_val` | text | maximum allowed value; NULL for non-numeric vartypes |
| `enumvals` | text[] | allowed values for enum vartype; NULL otherwise |
| `boot_val` | text | compiled-in default assumed at server start if not otherwise set |
| `reset_val` | text | value RESET would restore in the current session |
| `sourcefile` | text | config file that set current value; NULL unless querying role is superuser or has `pg_read_all_settings` |
| `sourceline` | int4 | line in that file; same visibility restriction |
| `pending_restart` | bool | `true` if value changed in configuration file but requires restart to take effect |

Other doc-verified facts you need for the console:
- `UPDATE pg_settings SET setting=... WHERE name=...` is equivalent to session-level `SET` only — it never persists. Not useful for the console's cluster-wide apply path.
- **Extension GUCs are invisible unless the defining module is loaded in the backend executing the query.** Libraries in `shared_preload_libraries` are loaded in every backend, so their GUCs always show. Modules loaded only in special processes (e.g. archive modules, PG15+) never show in a normal session. (PG17 doc, `pg_settings` page, last paragraph.)
- `source` column values (from `GucSource_Names`, `guc_tables.c`): `default`, `environment variable`, `configuration file`, `command line`, `global`, `database`, `user`, `database user`, `client`, `override`, `interactive`, `test`, `session`. Useful signal: **Patroni-forced parameters show `source = 'command line'`** (Patroni passes them as postmaster args); DCS-applied parameters show `source = 'configuration file'` with `sourcefile = .../postgresql.conf` (Patroni rewrites it).
- **Localization caveat**: `category`, `short_desc`, `extra_desc` pass through gettext (`config_group_names` entries are `gettext_noop`). If `lc_messages` is not English/C, category strings won't match your doc-link mapping. Mitigation: issue `SET lc_messages TO 'C';` in the snapshot session (context `superuser` — fine since the harness connects as `postgres`), or map doc pages by name prefix fallback.

### context values — exact semantics and apply-channel implications

Doc order = "decreasing difficulty of changing" (verbatim semantics from PG17 `view-pg-settings`, identical in PG13):

| context | GUC flag | can change? | how a change takes effect | console apply channel |
|---|---|---|---|---|
| `internal` | PGC_INTERNAL | never at runtime | fixed at compile time or initdb (`block_size`, `wal_segment_size`, `server_version`, `data_checksums`) | **read-only display** |
| `postmaster` | PGC_POSTMASTER | config file / command line only | **full server restart required**; changed-but-unapplied shows `pending_restart=true` | CR apply → DCS → Patroni marks pending_restart → operator-driven rolling restart (§5) |
| `sighup` | PGC_SIGHUP | config file | **reload**: SIGHUP to postmaster (forwarded to children); no restart | CR apply → DCS → Patroni reloads automatically within one `loop_wait` (~10s) |
| `superuser-backend` | PGC_SU_BACKEND | config file, or per-connection startup packet (`PGOPTIONS`) by superuser/SET-privileged user | never changes inside a live session; config change (after SIGHUP) affects **only new sessions** (e.g. `log_connections`) | CR apply (reload; new sessions only) — surface "affects new connections only" |
| `backend` | PGC_BACKEND | same, but any user | same as above (e.g. `post_auth_delay`) | same |
| `superuser` | PGC_SUSET | config file, or `SET` by superuser / (PG15+) user with granted `SET` privilege | immediate per-session via SET; config change affects existing sessions **only if no session-local SET** (e.g. `log_statement`, `lc_messages`, `deadlock_timeout`) | CR apply (reload) or per-session SET in pgbench runs |
| `user` | PGC_USERSET | anyone, anywhere | same as `superuser` but any user (e.g. `work_mem`, `random_page_cost`) | CR apply (reload) or per-session SET |

Note for PG13/14: docs say only superusers can SET `superuser`-context params (the grantable `SET` privilege / `pg_parameter_acl` arrived in PG15). Context strings themselves are unchanged 13→17.

---

## 2. GUC counts per major version; extension GUCs

Counted directly from the GUC tables in source (`guc.c` for 13–15, `guc_tables.c` for 16–17; regex `^\s*\{"name", PGC_`):

| PG | entries in source table | hidden (`GUC_NO_SHOW_ALL`) | debug-build-only (compiled out on stock builds) | **≈ rows in `pg_settings` on a stock Linux build** |
|---|---|---|---|---|
| 13 | 341 | 6 | ~9 | **~326** |
| 14 | 356 | 6 | ~9 | **~341** |
| 15 | 363 | 6 | ~9 | **~348** |
| 16 | 371 | 6 | ~9 | **~356** |
| 17 | 386 | 6 | ~9 | **~371** |

- The 6 hidden GUCs (identical 13–17): `default_with_oids`, `is_superuser`, `role`, `seed`, `session_authorization`, `ssl_renegotiation_limit`.
- The ~9 compiled-out ones: `log_btree_build_stats` (BTREE_BUILD_STATS), `trace_locks`/`trace_userlocks`/`trace_lwlocks`/`debug_deadlocks`/`trace_lock_oidmin`/`trace_lock_table` (LOCK_DEBUG), `trace_syncscan` (TRACE_SYNCSCAN), `wal_debug` (WAL_DEBUG). `trace_sort` IS present on stock builds (TRACE_SORT defined by default in `pg_config_manual.h`).
- Counts drift within minor releases occasionally; **this is exactly why the console must introspect, not embed a list.** Treat these numbers as sanity bounds only (e.g. warn if a snapshot returns < 300 rows).
- Cross-version deltas matter for diff UX (~15 params added per major; removals too: PG17 dropped `db_user_namespace`, `old_snapshot_threshold`, `trace_recovery_messages`; added `allow_alter_system`, `io_combine_limit`, `summarize_wal`, etc.). Derive add/remove sets by diffing snapshots, never hardcode.

### How extensions add GUCs
- Custom GUCs have **two-part names** (`extension.param`). PostgreSQL accepts a `SET` of *any* two-part name as a **placeholder** (vartype `string`, category `Customized Options`) even when the module isn't loaded; when the module loads (`shared_preload_libraries`, C-function call, or `LOAD`), `DefineCustomXxxVariable()` converts placeholders to typed GUCs and **removes unrecognized placeholders under its prefix with a warning** (PG17 `runtime-config-custom` sect1, verified verbatim).
- Loaded custom GUCs get real `vartype`, `min_val`/`max_val`, `enumvals`, `boot_val`, `context` — your introspection query handles them for free. Category is always `Customized Options`.
- Concrete counts: `pg_stat_statements` defines exactly **5** (verified in REL_17 `pg_stat_statements.c`): `.max` (integer), `.track` (enum `none|top|all`), `.track_utility` (bool), `.track_planning` (bool), `.save` (bool). `auto_explain` ~13 (`auto_explain.log_*`), `pgaudit` ~11 (`pgaudit.log*`, `.role`), `pg_stat_monitor` ~15 (`pg_stat_monitor.pgsm_*`), pgvector adds a few (`ivfflat.probes`, `hnsw.ef_search`, ...).
- **Percona v2 built-in extensions** (`spec.extensions.builtin.{pg_stat_statements,pg_stat_monitor,pg_audit}`): the operator appends to `shared_preload_libraries` and **enforces** (Mandatory, non-overridable while enabled): `pg_stat_statements.track=all`; `pg_stat_monitor.pgsm_query_max_len=2048`, `pg_stat_monitor.pgsm_enable_overflow=off` (verified in `ppg/internal/pgstatstatements/pgstatstatement.go`, `ppg/internal/pgstatmonitor/pgstatmonitor.go`, and `k8spg-docs/docs/immutable-options.md`).

---

## 3. vartype + unit semantics; UI rendering/validation/conversion

**vartype closed set** (from `config_type_names`, not localized): `bool`, `integer`, `real`, `string`, `enum`.

**`unit` column closed set** (from `get_config_unit_name()`, REL_17 `guc.c:2816`, same set in 13–17): `NULL`, `B`, `kB`, `MB`, `8kB` (rendered as `%dkB` from BLCKSZ/1024 or XLOG_BLCKSZ/1024 — would read e.g. `16kB` on a nonstandard-block build; **parse it as `<N>kB`, don't string-match `8kB`**), `ms`, `s`, `min`. There is no `GB/TB/us/h/d` **base** unit in 13–17 — those exist only as *input* units. `real` GUCs can carry units too (e.g. `vacuum_cost_delay` unit `ms`).

**Input/validation rules** (PG17 `config.sgml` §"Parameter Names and Values", verified verbatim):
- **bool**: accepts `on,off,true,false,yes,no,1,0` case-insensitive, or any unambiguous prefix.
- **integer**: decimal, hex (`0x...`), octal (leading `0`); fractional input rounded to nearest int; no thousands separators.
- **numeric-with-unit**: value must be quoted as a string when a unit is given. Memory units: `B, kB, MB, GB, TB` (multiplier **1024**). Time units: `us, ms, s, min, h, d`. **Unit names are case-sensitive** (`MB` not `mb`); whitespace allowed between number and unit. Fractional value with unit rounds to a multiple of the next-smaller unit (`30.1GB` → `30822MB`), then final integer rounding.
- **enum**: values case-insensitive, must be in `enumvals`.
- **string**: quote-escape single quotes by doubling.

**Recommended UI treatment:**
- Widget by vartype: bool → toggle; enum → select fed from `enumvals`; integer/real → numeric input with unit dropdown when `unit` non-NULL; string → text.
- Canonical conversion: `bytes = setting::numeric * factor(unit)` with factor map `{B:1, kB:1024, MB:1048576, "<N>kB": N*1024}`; `ms = setting * {ms:1, s:1000, min:60000}`. Humanize for display (pick largest unit with integral or 1-decimal value). For the "current value" chip, `current_setting(name)` / `SHOW` already returns the humanized form (`128MB`) — cheaper than reimplementing.
- Validate client-side with `min_val`/`max_val` (parse as float64 — all integer GUCs are int32 range, reals are doubles). Remember many GUCs use **sentinel values** (`-1` = disabled, `0` = special); `min_val` already includes the sentinel so range-check alone is safe, but render a hint when value == min and min is negative.
- Write values into the CR **as strings exactly as the user entered them with units** (`shared_buffers: "2GB"`). Patroni passes them through to postgresql.conf; PostgreSQL does the unit conversion. Avoid pre-converting — it destroys round-trip readability of the CR. (Patroni's own validator parses units fine; PGO stringifies all scalar YAML values — `internal/patroni/postgres.go` comment block shows Patroni's exact coercion behavior.)
- Server-side dry-run validation for non-postmaster params: in a throwaway transaction run `SELECT set_config($name,$value,true)` then `ROLLBACK` — parse errors surface without persisting. postmaster-context params can't be test-set; rely on vartype/range/enum checks. This matters because **Percona docs state explicitly: "The Operator does not validate the options it passes to Patroni. Invalid values can make the cluster unavailable."** (`k8spg-docs/docs/options.md`). A wrong `shared_buffers` string will crash-loop the cluster — the console must be the validator.
- PG15+ bonus: `pg_settings_get_flags(name)` returns flags incl. `NOT_IN_SAMPLE`, `RUNTIME_COMPUTED` — optional polish for hiding developer options.

---

## 4. Parameters Patroni manages — the authoritative list

From Patroni master `patroni/postgresql/config.py`, `ConfigHandler.CMDLINE_OPTIONS` (verbatim, with default, validator, min PG version):

| parameter | Patroni default | validator / constraint | notes |
|---|---|---|---|
| `listen_addresses` | — | `_false_validator` → **user value always dropped** | derived from `postgresql.listen` / operator |
| `port` | — | dropped | derived from `postgresql.listen`; Crunchy/Percona: use `spec.port` |
| `cluster_name` | — | dropped | derived from Patroni `scope` (= cluster name) |
| `wal_level` | `hot_standby` | enum `hot_standby\|replica\|logical` | `minimal` rejected |
| `hot_standby` | `on` | must be true | forced on |
| `max_connections` | 100 | int ≥ 25 | shared-memory param |
| `max_wal_senders` | 10 | int ≥ 3 | shared-memory param (PG12+) |
| `wal_keep_segments` | 8 | int ≥ 1 | PG < 13 only |
| `wal_keep_size` | `128MB` | int ≥ 16 (MB) | PG 13+; **not** passed on cmdline |
| `max_prepared_transactions` | 0 | int ≥ 0 | shared-memory param |
| `max_locks_per_transaction` | 64 | int ≥ 32 | shared-memory param |
| `track_commit_timestamp` | `off` | bool | PG 9.5+ |
| `max_replication_slots` | 10 | int ≥ 4 | |
| `max_worker_processes` | 8 | int ≥ 2 | shared-memory param |
| `wal_log_hints` | `on` | bool | needed for pg_rewind |

Semantics (Patroni `patroni_configuration.rst`, "Important rules", verbatim claims):
- For these parameters, **values set in local Patroni config or env vars take no effect — they can only be changed via the DCS dynamic configuration** (`patronictl edit-config` / REST `PATCH /config`). In operator terms: only via the CR's dynamicConfiguration path, which the operator writes into DCS.
- Patroni writes them into `postgresql.conf` **and passes them as postmaster command-line arguments**, giving them precedence above everything **including `ALTER SYSTEM`** (exceptions: `wal_keep_segments`/`wal_keep_size` are not put on the command line).
- Two sub-groups: (a) must-be-identical on primary+replicas (`max_connections`, `max_locks_per_transaction`, `max_worker_processes`, `max_prepared_transactions`, `wal_level`, `track_commit_timestamp`); (b) restricted-to-DCS as a policy choice (`max_wal_senders`, `max_replication_slots`, `wal_keep_segments`/`wal_keep_size`, `wal_log_hints`). Plus operator-derived (`listen_addresses`, `port`, `cluster_name`, `hot_standby`).
- **Shared-memory params restart-ordering rule** (docs "PostgreSQL parameters that touch shared memory"): increase → restart replicas first, then primary; decrease → restart primary first, then replicas. On a replica whose `pg_controldata` still shows the higher value, Patroni **refuses to apply the decrease**: `effective_configuration` (config.py:1345+) substitutes the controldata value (`Current max_connections setting` etc.), starts with it, and sets `pending_restart` until the replica has replayed the primary's change. The affected set (controldata mapping): `max_connections`, `max_prepared_transactions`, `max_locks_per_transaction`, `max_worker_processes` (9.4+), `max_wal_senders` (12+).
- **pending_restart detection** (config.py:1302+): after any reload, Patroni waits 1s then queries `SELECT name, current_setting(name), unit, vartype FROM pg_settings WHERE pending_restart` (excluding params it just changed itself), records a per-param diff as `pending_restart_reason`, and sets the member flag. It also catches out-of-band edits ("seem to be changed bypassing Patroni config"). The flag is exposed via REST `GET /patroni` (`"pending_restart": true`), `patronictl list` (`Pending restart` column, and `patronictl list --extended` shows the reason), and — with Kubernetes DCS — **in the instance Pod's `status` annotation** (verified: PGO reads `pod.annotations["status"]` and substring-matches `"pending_restart":true` — `internal/patroni/reconcile.go:209`). Your console can read the same annotation with plain kubectl: `kubectl get pod X -o jsonpath='{.metadata.annotations.status}'`.
- Additional never-touch set: **recovery parameters** (`_RECOVERY_PARAMETERS`: `primary_conninfo`, `primary_slot_name`, `restore_command`, `recovery_min_apply_delay`, `recovery_target*`, ...) are written by Patroni on replicas; and `synchronous_standby_names` is Patroni-managed whenever `synchronous_mode` is on (dedicated code path `set_synchronous_standby_names`). `hba_file`/`ident_file`: if set non-default, Patroni stops managing pg_hba/pg_ident.
- Patroni 4.x adds role-scoped overrides in DCS: `postgresql.parameters_primary|parameters_replica|parameters_standby_leader` (merged over base `parameters`) — not exposed by either operator's spec today, but visible if someone hand-edits DCS.

---

## 5. How Percona v2 / Crunchy v5 apply parameters end-to-end

### CR field paths
- **Percona v2 (`perconapgcluster`, pgv2.percona.com)**: `spec.patroni.dynamicConfiguration.postgresql.parameters` (map, schemaless) and `...postgresql.pg_hba` (string list). Percona docs (`options.md`, `immutable-options.md`): **only these two subsections are honored**; all other keys under `patroni.dynamicConfiguration` (e.g. `loop_wait`, `use_slots`, `use_pg_rewind`) are ignored/overridden by the operator (`ttl` ← `spec.patroni.leaderLeaseDurationSeconds`, `loop_wait` ← `spec.patroni.syncPeriodSeconds`, `use_pg_rewind` forced true for PG>10, `use_slots` default false).
- **Crunchy v5.8**: two channels. Preferred: **`spec.config.parameters`** (map[string]IntOrString, `+kubebuilder:validation:MaxProperties=50`, granular map type) with **CEL admission rules** (full list below). Legacy: `spec.patroni.dynamicConfiguration.postgresql.parameters` (schemaless, no admission validation). Merge precedence in `generatePostgresParameters` (verified v5.8.8, `internal/controller/postgrescluster/postgres.go:123`): **mandatory(operator) > spec.config.parameters > spec.patroni.dynamicConfiguration.postgresql.parameters > operator defaults**. `shared_preload_libraries` is merged, not overwritten (Crunchy: mandatory libs first, then user libs; Percona K8SPG-442: user libs first, then mandatory; both force `citus` to the front if present).

### Crunchy `spec.config.parameters` CEL-forbidden list (v5.8.8 — the best seed for your static "forbidden" overlay)
`config_file`, `data_directory`, `external_pid_file`, `hba_file`, `ident_file`, `listen_addresses`, `port` (→ `spec.port`), `ssl` and all `ssl_*` except `ssl_groups`/`ssl_ecdh_curve`, all `unix_socket_*`, `wal_level` (only `"logical"` accepted if present), `wal_log_hints`, `archive_mode`, `archive_command`, `restore_command`, `recovery_target` and all `recovery_target_*`, `hot_standby`, `synchronous_standby_names`, `primary_conninfo`, `primary_slot_name`, `recovery_min_apply_delay`, `cluster_name`, `logging_collector`, `log_file_mode`.

### Operator-enforced (Mandatory — reverted on reconcile; from code + Percona `immutable-options.md`)
| parameter | value | who |
|---|---|---|
| `ssl`, `ssl_cert_file`, `ssl_key_file`, `ssl_ca_file` | `on`, `/pgconf/tls/{tls.crt,tls.key,ca.crt}` | both |
| `unix_socket_directories` | `/tmp/postgres` | both |
| `log_file_mode` | `0660` | Crunchy mandatory; Percona docs list it too |
| `archive_mode` | `on` (kept `on` even when backups disabled — `archive_command` becomes `true` to discard WAL and avoid a restart) | both (pgBackRest) |
| `archive_command` | `pgbackrest --stanza=db archive-push "%p"` (Percona ≥2.8 optionally wraps with commit-timestamp extraction) | both |
| `restore_command` | `pgbackrest --stanza=db archive-get %f "%p"` — **Percona: user override via dynamicConfiguration is honored** (code reads user value before adding Mandatory); Crunchy: forbidden in spec.config | pgBackRest |
| `track_commit_timestamp` | `true` — **Percona only** (when `backups.trackLatestRestorableTime`, default true pre-2.8). Note this is a Patroni CMDLINE_OPTIONS param with postmaster context: it works because the operator injects it via DCS | Percona |
| `wal_level` | `logical` — **Crunchy v5.8 Mandatory (cannot override)**; **Percona: Default only — overridable** to `replica`/`logical` (never `minimal`, Patroni enum rejects it) | differs! |
| extension params | see §2 (Percona built-ins) | Percona |

### Operator defaults (overridable)
`jit=off`, `password_encryption=scram-sha-256`, `archive_timeout=60s`, `huge_pages=try|off` (computed from hugepages resource limits), Percona `wal_level=logical`, Crunchy main also `log_directory`.

### Apply pipeline mechanics (code-verified)
1. **Bootstrap**: merged config rendered into Patroni YAML `bootstrap.dcs` (only used at first cluster init; ignored afterward) — `internal/patroni/config.go`.
2. **Every reconcile after bootstrap**: operator execs into one running instance pod and runs **`patronictl edit-config --replace=- --force`** with the fully merged JSON on stdin (`Executor.ReplaceConfiguration`, `internal/patroni/api.go:126`; caller `reconcilePatroniDynamicConfiguration`). This **replaces the entire DCS dynamic config** — hence hand-made `patronictl edit-config` changes are wiped on next reconcile (Percona docs confirm; escape hatch = Percona "unmanaged" mode / paused reconciliation).
3. **Propagation**: each node's Patroni HA loop (every `loop_wait`, default 10s) sees the new DCS config → rewrites `postgresql.conf` → `pg_ctl reload` (SIGHUP) → sighup/user-context params take effect immediately.
4. **Restart-needed params**: Patroni sets `pending_restart` per §4. The operator's `handlePatroniRestarts` (`internal/controller/postgrescluster/patroni.go:49`) sees the pod `status` annotation and runs **`patronictl restart --pending --force --role=primary <scope>`** if the primary needs it, else `--role=replica` for replicas — i.e. an automatic, Patroni-coordinated rolling restart, replicas typically first. The "decrease shared-memory param" case can leave replicas flagged until replay catches up (comment cites zalando/patroni config.py L1069 behavior). **Both operators do this identically** (Percona fork verified).
5. `restartedAt`-style pod-template annotation changes (spec.metadata.annotations) cause a StatefulSet rolling update — a *different*, heavier restart channel; not used for parameter changes.

**Console verification loop after an apply**: (a) `kubectl exec <pod> -c database -- patronictl show-config` to confirm DCS content; (b) poll `pg_settings` for `setting` + `pending_restart`; (c) watch pod `status` annotations until no member reports `"pending_restart":true`; (d) compare `pg_postmaster_start_time()` before/after to prove the rolling restart actually happened.

---

## 6. Snapshot query, doc links, and the recommended registry design

### The snapshot query (source of truth)
Run on the current primary (kubectl exec → psql over the unix socket as `postgres`). JSON aggregation avoids all delimiter/quoting issues with `-At`:

```sql
SET lc_messages TO 'C';  -- stabilize category/short_desc for doc-link mapping
SELECT current_setting('server_version_num')::int AS version_num;
SELECT jsonb_build_object(
  'version_num', current_setting('server_version_num')::int,
  'taken_at',    now(),
  'params', (
    SELECT jsonb_agg(jsonb_build_object(
      'name', name, 'setting', setting, 'unit', unit,
      'category', category, 'short_desc', short_desc, 'extra_desc', extra_desc,
      'context', context, 'vartype', vartype, 'source', source,
      'min_val', min_val, 'max_val', max_val, 'enumvals', to_jsonb(enumvals),
      'boot_val', boot_val, 'reset_val', reset_val,
      'sourcefile', sourcefile, 'sourceline', sourceline,
      'pending_restart', pending_restart,
      'pretty', current_setting(name, true)   -- humanized value, missing_ok
    ) ORDER BY name)
    FROM pg_settings
  )
);
```
Invocation: `kubectl exec <primary-pod> -c database -- psql -U postgres -AtXq -c "<sql>"` (one line, or `-f -` with stdin). Complementary snapshots for the console: `pg_file_settings` (file entries + `error` column — shows values that failed to apply), `pg_hba_file_rules`, `pg_db_role_setting` (per-DB/role overrides that shadow cluster config), and `SELECT * FROM pg_settings WHERE pending_restart` as the cheap drift poll. Caveats: run against **primary and one replica** if you want to display divergence; extension GUCs require the module loaded (shared_preload ones always are). `postgres --describe-config` exists for offline bootstrap but is unnecessary given live clusters.

### Doc-link scheme
`https://www.postgresql.org/docs/<major>/<page>.html#GUC-<NAME>` where `<major> = version_num / 10000` and `<NAME> = upper(replace(name,'_','-'))` (e.g. `#GUC-MAX-WAL-SIZE`). `<page>` from the first segment of `category` (segments split on ` / `); the full first-segment set (verified from `config_group_names`, stable enough across 13–17 — PG15 renamed some *second* segments only):

| category first segment | page slug |
|---|---|
| File Locations | `runtime-config-file-locations` |
| Connections and Authentication | `runtime-config-connection` |
| Resource Usage | `runtime-config-resource` |
| Write-Ahead Log | `runtime-config-wal` |
| Replication | `runtime-config-replication` |
| Query Tuning | `runtime-config-query` |
| Reporting and Logging | `runtime-config-logging` |
| Statistics | `runtime-config-statistics` |
| Autovacuum | `runtime-config-autovacuum` |
| Client Connection Defaults | `runtime-config-client` |
| Lock Management | `runtime-config-locks` |
| Version and Platform Compatibility | `runtime-config-compatible` |
| Error Handling | `runtime-config-error-handling` |
| Preset Options | `runtime-config-preset` |
| Customized Options | `runtime-config-custom` |
| Developer Options | `runtime-config-developer` |
| Ungrouped | (none in practice) |

For dotted names, map prefix → extension page instead: `pg_stat_statements.*` → `/docs/<major>/pgstatstatements.html`, `auto_explain.*` → `auto-explain.html`, `plpgsql.*` → `plpgsql.html`; non-core prefixes → external (`pg_stat_monitor.*` → Percona docs, `pgaudit.*` → github.com/pgaudit); unknown prefix → no link. Keep the prefix map in the static overlay.

### Recommended registry design
**Live introspection = source of truth; a tiny static overlay = policy.** Concretely:

1. **Live layer** (per target cluster): the snapshot above, cached keyed by `(cluster, server_version_num, sorted shared_preload_libraries)`; TTL ~5–15 min; force-refresh after every config apply and on demand. This gives you the complete, always-correct set — names, types, bounds, enums, units, defaults, current values, restart-needed flags — for whatever major version and extension set the cluster actually runs. Zero hand-typed parameter data.

2. **Static overlay** (small JSON, ~60 entries, version-independent; name or prefix keyed) adding only what pg_settings cannot know:
   - `apply_channel` classification, evaluated in priority order:
     - `forbidden` — operator-enforced or identity params; console renders read-only with reason. Seed = union of: Crunchy CEL list (§5), operator Mandatory sets (ssl*, unix_socket_*, archive_*, restore_command†, log_file_mode, track_commit_timestamp†), Patroni-dropped (`listen_addresses`, `port`, `cluster_name`, `hot_standby`), Patroni recovery params + `synchronous_standby_names`. † = per-operator flag (Percona allows restore_command override; Percona allows `wal_level: replica`, Crunchy doesn't).
     - `patroni_coordinated` — settable via CR but DCS-special: `max_connections`, `max_worker_processes`, `max_locks_per_transaction`, `max_prepared_transactions`, `max_wal_senders`, `max_replication_slots`, `wal_keep_size`(/`wal_keep_segments`), `wal_log_hints`, `track_commit_timestamp`, `wal_level`. UI: show Patroni minimums (25/2/32/0/3/4/16MB) *in addition to* pg bounds, show "must be identical on all members", warn on decreases (two-phase restart semantics), and expect an automatic rolling restart.
     - `special_editor` — `shared_preload_libraries` (list editor; operator appends its own libs — display effective merged value from pg_settings vs CR value), `search_path`, `archive_timeout`.
     - everything else: derive channel live from `context` → `restart_rolling` (postmaster), `reload` (sighup), `reload_new_sessions_only` (backend/superuser-backend), `reload_or_session` (user/superuser), `read_only` (internal + category `Preset Options`).
   - `ownership` tag for provenance display: `operator-tls`, `operator-backup(pgbackrest)`, `patroni-ha`, `extension:<name>`, `user`.
   - doc-link prefix map for extensions (above).
3. **Write path**: single supported channel = CR patch. Percona: `spec.patroni.dynamicConfiguration.postgresql.parameters.<name>`. Crunchy: `spec.config.parameters.<name>` (fall back to dynamicConfiguration for pre-5.6 clusters); values as strings with units verbatim. Pre-apply validation: vartype/range/enum/unit client-side + `set_config` dry-run for settable contexts + overlay policy check. Post-apply: verify loop from §5.
4. **Drift & status UI**: per-parameter badge from `source` (`command line` = Patroni-forced, `configuration file` + sourcefile postgresql.conf = DCS-applied, `default` = untouched); cluster-level "restart pending" banner from pod `status` annotations; "external override" warning when `pg_db_role_setting` rows or `ALTER SYSTEM` (`postgresql.auto.conf` sourcefile) shadow the CR value.
5. **Sanity checks instead of counts**: assert snapshot row count within [300, 500], all 17 columns present, and all overlay `forbidden`/`patroni_coordinated` names resolve against the live set (names that stop resolving indicate a PG version drop — surface, don't crash).

<!-- MACHINE_CATALOG -->
```json
{
  "pg_settings_columns": ["name","setting","unit","category","short_desc","extra_desc","context","vartype","source","min_val","max_val","enumvals","boot_val","reset_val","sourcefile","sourceline","pending_restart"],
  "identical_across": ["13","14","15","16","17"],
  "contexts": {
    "internal":  {"changeable": false, "apply": "read_only"},
    "postmaster":{"changeable": true,  "apply": "restart"},
    "sighup":    {"changeable": true,  "apply": "reload"},
    "superuser-backend": {"changeable": true, "apply": "reload_new_sessions_only", "session_set_at_connect": "superuser_or_set_priv"},
    "backend":   {"changeable": true,  "apply": "reload_new_sessions_only", "session_set_at_connect": "any_user"},
    "superuser": {"changeable": true,  "apply": "reload_or_session_set", "session_set": "superuser_or_set_priv_pg15plus"},
    "user":      {"changeable": true,  "apply": "reload_or_session_set", "session_set": "any_user"}
  },
  "vartypes": ["bool","enum","integer","real","string"],
  "base_units": ["B","kB","MB","8kB","ms","s","min"],
  "input_units": {"memory": ["B","kB","MB","GB","TB"], "memory_multiplier": 1024, "time": ["us","ms","s","min","h","d"], "case_sensitive": true},
  "unit_factors_bytes": {"B":1,"kB":1024,"8kB":8192,"MB":1048576},
  "unit_factors_ms": {"ms":1,"s":1000,"min":60000},
  "source_values": ["default","environment variable","configuration file","command line","global","database","user","database user","client","override","interactive","test","session"],
  "guc_counts": {"13":{"table_entries":341,"visible_approx":326},"14":{"table_entries":356,"visible_approx":341},"15":{"table_entries":363,"visible_approx":348},"16":{"table_entries":371,"visible_approx":356},"17":{"table_entries":386,"visible_approx":371},"hidden_no_show_all":["default_with_oids","is_superuser","role","seed","session_authorization","ssl_renegotiation_limit"]},
  "patroni_cmdline_options": {
    "listen_addresses": {"policy":"dropped","source":"postgresql.listen"},
    "port": {"policy":"dropped","source":"postgresql.listen"},
    "cluster_name": {"policy":"dropped","source":"scope"},
    "hot_standby": {"policy":"forced","value":"on"},
    "wal_level": {"policy":"enum","allowed":["hot_standby","replica","logical"],"default":"hot_standby"},
    "max_connections": {"policy":"min","min":25,"default":100,"shared_memory":true},
    "max_wal_senders": {"policy":"min","min":3,"default":10,"shared_memory":true},
    "wal_keep_segments": {"policy":"min","min":1,"default":8,"pg_max":"12"},
    "wal_keep_size": {"policy":"min","min_mb":16,"default":"128MB","pg_min":"13","not_on_cmdline":true},
    "max_prepared_transactions": {"policy":"min","min":0,"default":0,"shared_memory":true},
    "max_locks_per_transaction": {"policy":"min","min":32,"default":64,"shared_memory":true},
    "track_commit_timestamp": {"policy":"bool","default":"off"},
    "max_replication_slots": {"policy":"min","min":4,"default":10},
    "max_worker_processes": {"policy":"min","min":2,"default":8,"shared_memory":true},
    "wal_log_hints": {"policy":"bool","default":"on"}
  },
  "patroni_recovery_params_never_set": ["primary_conninfo","primary_slot_name","restore_command_on_replica","recovery_min_apply_delay","recovery_target","recovery_target_*","synchronous_standby_names"],
  "operator_forbidden": {
    "both": ["ssl","ssl_cert_file","ssl_key_file","ssl_ca_file","unix_socket_directories","archive_mode","archive_command","listen_addresses","port","cluster_name","hot_standby","config_file","data_directory","external_pid_file","hba_file","ident_file","log_file_mode"],
    "crunchy_extra_cel": ["ssl_*(except ssl_groups,ssl_ecdh_curve)","unix_socket_*","wal_level!=logical","wal_log_hints","restore_command","recovery_target*","synchronous_standby_names","primary_conninfo","primary_slot_name","recovery_min_apply_delay","logging_collector"],
    "percona_notes": {"restore_command":"overridable via dynamicConfiguration","wal_level":"overridable to replica|logical","track_commit_timestamp":"enforced true when trackLatestRestorableTime"}
  },
  "operator_defaults_overridable": {"wal_level_percona":"logical","jit":"off","password_encryption":"scram-sha-256","archive_timeout":"60s","huge_pages":"try|off computed"},
  "cr_paths": {
    "percona_v2": "spec.patroni.dynamicConfiguration.postgresql.parameters",
    "crunchy_v5_preferred": "spec.config.parameters (MaxProperties=50, CEL validated)",
    "crunchy_v5_legacy": "spec.patroni.dynamicConfiguration.postgresql.parameters",
    "crunchy_precedence": "mandatory > spec.config.parameters > dynamicConfiguration > defaults"
  },
  "apply_mechanics": {
    "post_bootstrap": "operator exec: patronictl edit-config --replace=- --force (full DCS replace, every reconcile)",
    "reload": "patroni HA loop <= loop_wait(10s) -> rewrite postgresql.conf -> SIGHUP",
    "pending_restart_signal": "pod metadata.annotations.status contains \"pending_restart\":true (k8s DCS)",
    "rolling_restart": "operator exec: patronictl restart --pending --force --role=primary|replica <scope>",
    "shared_memory_decrease": "replica keeps pg_controldata value + pending_restart until replay catches up"
  },
  "doc_link": {
    "template": "https://www.postgresql.org/docs/{major}/{page}.html#GUC-{NAME_UPPER_HYPHEN}",
    "category_to_page": {"File Locations":"runtime-config-file-locations","Connections and Authentication":"runtime-config-connection","Resource Usage":"runtime-config-resource","Write-Ahead Log":"runtime-config-wal","Replication":"runtime-config-replication","Query Tuning":"runtime-config-query","Reporting and Logging":"runtime-config-logging","Statistics":"runtime-config-statistics","Autovacuum":"runtime-config-autovacuum","Client Connection Defaults":"runtime-config-client","Lock Management":"runtime-config-locks","Version and Platform Compatibility":"runtime-config-compatible","Error Handling":"runtime-config-error-handling","Preset Options":"runtime-config-preset","Customized Options":"runtime-config-custom","Developer Options":"runtime-config-developer"},
    "extension_prefixes": {"pg_stat_statements":"pgstatstatements.html","auto_explain":"auto-explain.html","plpgsql":"plpgsql.html"}
  }
}
```
