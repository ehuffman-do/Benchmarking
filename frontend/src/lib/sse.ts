// Live cockpit transport. Wraps EventSource over the existing /runs/:id/stream.
// Each (re)connection begins with a `hello` event — the client treats that as a
// full reset and catches up from offset 0, so auto-reconnect never duplicates.

export interface Hello {
  run_id: string;
  mode: string;
  status: string;
  budget_s: number;
  start_utc: string;   // run t=0 wall-clock anchor (soak load start, else created) for alignment
}

export interface Progress {
  status: string;
  elapsed_s: number;
  budget_s: number;
  levels_total: number;
  levels_done: number;
  current: string;
}

export interface SampleBatch {
  file: string;
  header: string;
  offset: number;
  rows: string[];
}

export interface StreamHandlers {
  onHello?: (h: Hello) => void;
  onLog?: (chunk: string) => void;
  onSamples?: (b: SampleBatch) => void;
  onPg?: (b: SampleBatch) => void;
  onProgress?: (p: Progress) => void;
  onDone?: (d: { status: string }) => void;
  onError?: () => void;
}

export function openStream(runId: string, h: StreamHandlers): EventSource {
  const es = new EventSource(`/runs/${encodeURIComponent(runId)}/stream`);
  es.addEventListener("hello", (e) => h.onHello?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("log", (e) => h.onLog?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("samples", (e) => h.onSamples?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("pg", (e) => h.onPg?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("progress", (e) => h.onProgress?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => h.onError?.();
  return es;
}

// ── task jobs (preflight / prepare / doctor) ────────────────────────────

export interface CheckEvent {
  name: string;
  status: string; // ok | warn | fail | info
  detail: string;
}

export interface JobHandlers {
  onCheck?: (c: CheckEvent) => void;
  onLog?: (line: string) => void;
  onDone?: (d: { status: string }) => void;
  onError?: () => void;
}

export function openJobStream(jobId: number, h: JobHandlers): EventSource {
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.addEventListener("check", (e) => h.onCheck?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("log", (e) => h.onLog?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => h.onError?.();
  return es;
}

// ── series model ──────────────────────────────────────────────────────
// Columnar buffers for uPlot: t (elapsed seconds) + the five harness metrics.
// Sweep samples.csv uses `t_offset`; soak_timeseries.csv uses `t`.

export interface Series {
  t: number[];
  tps: number[];
  qps: number[];
  p99: number[];
  err: number[];
  reconn: number[];
  qpsR: number[];
  qpsW: number[];
  qpsO: number[];
}

export function emptySeries(): Series {
  return { t: [], tps: [], qps: [], p99: [], err: [], reconn: [], qpsR: [], qpsW: [], qpsO: [] };
}

export function appendBatch(s: Series, batch: SampleBatch): void {
  const cols = batch.header.split(",");
  const ix = (name: string) => cols.indexOf(name);
  const it = ix("t_offset") >= 0 ? ix("t_offset") : ix("t");
  const itps = ix("tps"), iqps = ix("qps"), ip99 = ix("lat_p99");
  const ierr = ix("err_s"), irec = ix("reconn_s");
  const ir = ix("r") >= 0 ? ix("r") : ix("qps_r");   // sweep samples.csv: r/w/o; soak: qps_r/w/o
  const iw = ix("w") >= 0 ? ix("w") : ix("qps_w");
  const io = ix("o") >= 0 ? ix("o") : ix("qps_o");
  for (const line of batch.rows) {
    const f = line.split(",");
    const t = parseFloat(f[it]);
    if (Number.isNaN(t)) continue;
    s.t.push(t);
    s.tps.push(num(f[itps]));
    s.qps.push(num(f[iqps]));
    s.p99.push(num(f[ip99]));
    s.err.push(num(f[ierr]));
    s.reconn.push(num(f[irec]));
    s.qpsR.push(numOrNaN(f[ir]));
    s.qpsW.push(numOrNaN(f[iw]));
    s.qpsO.push(numOrNaN(f[io]));
  }
}

function num(v: string | undefined): number {
  const n = v === undefined ? NaN : parseFloat(v);
  return Number.isNaN(n) ? 0 : n;
}

// For rate columns that can be a blank GAP (counter reset / not-captured): keep
// NaN so the chart line breaks rather than plotting a misleading 0.
function numOrNaN(v: string | undefined): number {
  return v === undefined || v === "" ? NaN : parseFloat(v);
}

// ── engine-side PostgreSQL metrics (live sampler) ───────────────────────
// pg_timeseries.csv columns (header-indexed, so order/extra columns are safe):
//   t,active,total_conn,xacts_s,commits_s,rollbacks_s,cache_hit_pct,
//   blks_read_s,blks_hit_s,wal_mb_s,tup_*_s,deadlocks_s,conflicts_s,
//   temp_bytes_s,temp_files_s,ckpt_*_s,bgw_*_s,repl_replay_lag_s

export interface PgSeries {
  t: number[];
  active: number[]; blockedQueries: number[]; lockWaitMaxS: number[];
  cacheHit: number[]; walMbs: number[]; xactsS: number[];
  commitsS: number[]; rollbacksS: number[];
  blksReadS: number[]; blksHitS: number[];
  tupReturnedS: number[]; tupFetchedS: number[];
  tupInsertedS: number[]; tupUpdatedS: number[]; tupDeletedS: number[];
  deadlocksS: number[]; tempBytesS: number[];
  ckptWriteMsS: number[]; ckptSyncMsS: number[];
  bgwCleanS: number[]; bgwAllocS: number[];
  replLagS: number[];
}

export function emptyPg(): PgSeries {
  return {
    t: [], active: [], blockedQueries: [], lockWaitMaxS: [],
    cacheHit: [], walMbs: [], xactsS: [],
    commitsS: [], rollbacksS: [], blksReadS: [], blksHitS: [],
    tupReturnedS: [], tupFetchedS: [], tupInsertedS: [], tupUpdatedS: [], tupDeletedS: [],
    deadlocksS: [], tempBytesS: [], ckptWriteMsS: [], ckptSyncMsS: [],
    bgwCleanS: [], bgwAllocS: [], replLagS: [],
  };
}

export function appendPg(s: PgSeries, batch: SampleBatch): void {
  const cols = batch.header.split(",");
  const ix = (name: string) => cols.indexOf(name);
  const get = (f: string[], name: string) => numOrNaN(f[ix(name)]);
  for (const line of batch.rows) {
    const f = line.split(",");
    const t = parseFloat(f[ix("t")]);
    if (Number.isNaN(t)) continue;
    s.t.push(t);
    s.active.push(num(f[ix("active")]));                 // gauge
    s.blockedQueries.push(num(f[ix("blocked_queries")]));
    s.lockWaitMaxS.push(get(f, "lock_wait_max_s"));
    s.cacheHit.push(get(f, "cache_hit_pct"));
    s.walMbs.push(get(f, "wal_mb_s"));
    s.xactsS.push(get(f, "xacts_s"));
    s.commitsS.push(get(f, "commits_s"));
    s.rollbacksS.push(get(f, "rollbacks_s"));
    s.blksReadS.push(get(f, "blks_read_s"));
    s.blksHitS.push(get(f, "blks_hit_s"));
    s.tupReturnedS.push(get(f, "tup_returned_s"));
    s.tupFetchedS.push(get(f, "tup_fetched_s"));
    s.tupInsertedS.push(get(f, "tup_inserted_s"));
    s.tupUpdatedS.push(get(f, "tup_updated_s"));
    s.tupDeletedS.push(get(f, "tup_deleted_s"));
    s.deadlocksS.push(get(f, "deadlocks_s"));
    s.tempBytesS.push(get(f, "temp_bytes_s"));
    s.ckptWriteMsS.push(get(f, "ckpt_write_ms_s"));
    s.ckptSyncMsS.push(get(f, "ckpt_sync_ms_s"));
    s.bgwCleanS.push(get(f, "bgw_clean_s"));
    s.bgwAllocS.push(get(f, "bgw_alloc_s"));
    s.replLagS.push(get(f, "repl_replay_lag_s"));
  }
}

// ── cluster ops runs ────────────────────────────────────────────────────

export interface OpsEvent {
  ts_utc: string;
  ts_epoch_ms: number;
  type: string;
  label: string;
  note: string;
}

export interface OpsCsvBatch {
  file: string;
  header: string;
  offset: number;
  rows: string[];
}

export interface OpsStatus {
  ts_utc?: string;
  phase?: string;
  leader?: string;
  timeline?: number;
  ready?: string;
  members?: { name: string; role: string; state: string }[];
  [k: string]: unknown;
}

export interface OpsStreamHandlers {
  onHello?: (h: { op_run_id: string; op: string; status: string }) => void;
  onLog?: (chunk: string) => void;
  onEvents?: (b: { offset: number; items: OpsEvent[] }) => void;
  onStatus?: (s: OpsStatus) => void;
  onCsv?: (b: OpsCsvBatch) => void;
  onProgress?: (p: { status: string }) => void;
  onDone?: (d: { status: string }) => void;
  onError?: () => void;
}

export function openOpsStream(opRunId: string, h: OpsStreamHandlers): EventSource {
  const es = new EventSource(`/ops/runs/${encodeURIComponent(opRunId)}/stream`);
  es.addEventListener("hello", (e) => h.onHello?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("log", (e) => h.onLog?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("events", (e) => h.onEvents?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("status", (e) => h.onStatus?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("csv", (e) => h.onCsv?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("progress", (e) => h.onProgress?.(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("done", (e) => {
    h.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => h.onError?.();
  return es;
}
