import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { api } from "./api";
import type { Me } from "./types";
import { Shell } from "./components/Shell";
import { History } from "./pages/History";
import { RunDetail } from "./pages/RunDetail";
import { ReportView } from "./pages/ReportView";
import { Targets } from "./pages/Targets";
import { NewRun } from "./pages/NewRun";
import { JobView } from "./pages/JobView";
import { Diagnostics } from "./pages/Diagnostics";
import { Tasks } from "./pages/Tasks";
import { Compare } from "./pages/Compare";
import { LiveCompare } from "./pages/LiveCompare";
import { Settings } from "./pages/Settings";
import { Users } from "./pages/Users";
import { Audit } from "./pages/Audit";
import { ClusterOps } from "./pages/ClusterOps";
import { KubeTargetView } from "./pages/KubeTargetView";
import { KubeParams } from "./pages/KubeParams";
import { KubeDiag } from "./pages/KubeDiag";
import { KubeOperate } from "./pages/KubeOperate";
import { OpsRuns } from "./pages/OpsRuns";
import { OpsRunView } from "./pages/OpsRunView";
import { OpsCompare } from "./pages/OpsCompare";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Me>("/api/me").then(setMe).catch((e) => setErr(e.message));
  }, []);

  if (err) {
    return (
      <div className="login-wrap">
        <div className="card login-card">
          <h1>Console unavailable</h1>
          <p className="subtle">{err}</p>
          <a className="btn" href="/login">Go to sign in</a>
        </div>
      </div>
    );
  }
  if (!me) {
    return <div className="login-wrap"><div className="subtle mono">loading console…</div></div>;
  }

  return (
    <BrowserRouter basename="/ui">
      <Shell me={me}>
        <Routes>
          <Route path="/" element={<History me={me} />} />
          <Route path="/new" element={<NewRun me={me} />} />
          <Route path="/targets" element={<Targets me={me} />} />
          <Route path="/diagnostics" element={<Diagnostics />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/jobs/:jobId" element={<JobView />} />
          <Route path="/ops" element={<ClusterOps me={me} />} />
          <Route path="/ops/targets/:targetId" element={<KubeTargetView me={me} />} />
          <Route path="/ops/targets/:targetId/params" element={<KubeParams me={me} />} />
          <Route path="/ops/targets/:targetId/diag" element={<KubeDiag me={me} />} />
          <Route path="/ops/targets/:targetId/operate" element={<KubeOperate me={me} />} />
          <Route path="/ops/runs" element={<OpsRuns />} />
          <Route path="/ops/runs/:opRunId" element={<OpsRunView />} />
          <Route path="/ops/compare" element={<OpsCompare />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="/compare/live" element={<LiveCompare />} />
          {me.role === "admin" && <Route path="/settings" element={<Settings />} />}
          {me.role === "admin" && <Route path="/users" element={<Users me={me} />} />}
          {me.role === "admin" && <Route path="/audit" element={<Audit />} />}
          <Route path="/runs/:runId" element={<RunDetail me={me} />} />
          <Route path="/runs/:runId/report" element={<ReportView me={me} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
