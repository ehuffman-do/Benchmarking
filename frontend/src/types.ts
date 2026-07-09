// Shared API shapes. Kept in lock-step with the FastAPI JSON responses.

export type Role = "viewer" | "operator" | "admin";

export interface Me {
  user: string;
  role: Role;
  version: string;
}

export interface Run {
  run_id: string;
  label: string;
  edition: string;
  tshirt_size: string;
  mode: string;
  workload_type: string;
  status: string;
  tags: string;
  ticket: string;
  owner: string;
  environment: string;
  peak_qps: number | null;
  created_utc: string;
  finished_utc: string;
  source: string;
  // host/cluster are surfaced in Phase 3 (targets); present-but-empty until then.
  target_host?: string;
}

export interface Target {
  id: number;
  name: string;
  host: string;
  port: number;
  dbname: string;
  dbuser: string;
  sslmode: string;
}

export interface Job {
  id: number;
  kind: string;
  state: string;
  run_id: string | null;
  requested_by: string;
  scheduled_utc: string | null;
  created_utc: string;
  started_utc?: string | null;
  finished_utc?: string | null;
  exit_code?: number | null;
  error: string | null;
}

export interface PrepareStats {
  loaded_units?: string;
  wall_s?: number;
  db_size_pretty?: string;
  load_mb_s?: number | null;
  load_threads?: number;
  started_utc?: string;
  finished_utc?: string;
  database?: string;
  target_host?: string;
}

// ── cluster ops ──

export interface KubeTarget {
  id: number;
  name: string;
  kubeconfig_path: string;
  kubeconfig_imported: boolean;
  context: string;
  namespace: string;
  cr_kind: string;
  cr_name: string;
  pguser_secret: string;
  pguser_secret_key: string;
  db_user: string;
  db_name: string;
  api_server: string;
  last_validated_utc: string | null;
  topology_utc: string | null;
  schedules_paused: boolean;
  schedules_paused_utc: string | null;
  created_utc: string;
}

export interface PatroniMember {
  name: string;
  host: string;
  role: string;
  state: string;
  timeline: number | null;
  lag_mb: number | null;
}

export interface Topology {
  collected_utc: string;
  namespace: string;
  cr_kind: string;
  cr_name: string;
  postgres_version?: string;
  patroni?: { leader: string; timeline: number | null; members: PatroniMember[]; error?: string };
  pods?: { instances: PodInfo[]; pgbouncer: PodInfo[]; backup_jobs: PodInfo[]; other: PodInfo[] };
  statefulsets?: { name: string; replicas: number | null; ready: number }[];
  services?: { name: string; type: string; cluster_ip: string }[];
  backups?: { schedules: { repo: string; schedules: Record<string, string> }[];
              manual: unknown; global: Record<string, string> };
  pgbackrest_info?: string;
}

export interface PodInfo {
  name: string;
  phase: string;
  ready: string;
  node: string;
  pod_ip: string;
}

export interface OpsRun {
  op_run_id: string;
  kind: string;
  kube_target_id: number | null;
  kube_target_name: string;
  label: string;
  params: Record<string, unknown>;
  status: string;
  linked_run_id: string;
  headline: Record<string, any>;
  created_utc: string;
  finished_utc: string;
}

export interface OpsRunDetail {
  meta: Record<string, any>;
  index: OpsRun | null;
  job_id: number | null;
  job_state: string | null;
  stitched: Record<string, any> | null;
  files: string[];
  raw_files: string[];
}

export interface OpsCompareRow {
  op_run_id: string;
  kind: string;
  label: string;
  target: string;
  created_utc: string;
  case: string;
  downtime_ms: number | null;
  detection_ms: number | null;
  flip: boolean | null;
  classification: string;
  tl_change: string;
  new_primary: string;
  backoff_tail_ms: number | null;
  full_ha_recovery_s: number | null;
  status: string;
}
