"use client";

import { useCallback, useEffect, useState } from "react";
import { SystemCheck, SystemStatus, getStatus, getSystemStatus } from "../../lib/api";

// Which installation this is, so the fictional demo is never mistaken for a customer's records.
const ENVIRONMENT: Record<string, string> = {
  demo: "Fictional demo", pilot: "Customer pilot", production: "Production", development: "Development",
};

const STATE: Record<SystemCheck["state"], { label: string; tone: string }> = {
  ok: { label: "OK", tone: "tf-status-open" },
  off: { label: "Not in use", tone: "" },
  attention: { label: "Needs attention", tone: "tf-status-attention" },
  problem: { label: "Problem", tone: "tf-status-error" },
};

export default function SystemPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [environment, setEnvironment] = useState<{ name: string; declared: boolean } | null>(null);
  useEffect(() => {
    getStatus().then((found) => {
      const named = found as { environment?: string; environment_declared?: boolean };
      if (named.environment) setEnvironment({ name: named.environment, declared: Boolean(named.environment_declared) });
    }).catch(() => setEnvironment(null));
  }, []);
  const load = useCallback(() => {
    getSystemStatus().then((found) => { setStatus(found); setError(null); }).catch((e) => setError((e as Error).message));
  }, []);
  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">System health</h1>
        {environment && <p className="mt-1 text-xs text-[#526273]">Environment:{" "}
          <span className="tf-status" data-testid="environment">{ENVIRONMENT[environment.name] ?? environment.name}</span>
          {!environment.declared && environment.name === "production" && <span className="ml-1">(APP_ENVIRONMENT not set)</span>}</p>}
        <p className="tf-page-description">Whether this installation can do its job: processing, backups and database version. Updates by itself.</p>
      </div>
      {status && <span className={`tf-status ${status.healthy ? "tf-status-open" : "tf-status-attention"}`}>{status.healthy ? "All good" : "Needs attention"}</span>}
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {status && <>
      <section className="tf-panel" aria-label="Health checks">
        <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Check</th><th>State</th><th>Detail</th></tr></thead>
          <tbody>{status.checks.map((check) => <tr key={check.name}>
            <td className="whitespace-nowrap font-semibold">{check.name}</td>
            <td><span className={`tf-status ${STATE[check.state].tone}`}>{STATE[check.state].label}</span></td>
            <td>{check.message}{check.action && <span className="mt-0.5 block font-mono text-[11px] text-[#526273]">{check.action}</span>}</td>
          </tr>)}</tbody>
        </table></div>
      </section>
      <section className="tf-panel" aria-label="Processing queue">
        <div className="tf-panel-heading"><h2 className="tf-panel-title">Processing queue</h2></div>
        <dl className="grid grid-cols-2 gap-4 p-5 text-[13px] md:grid-cols-4">
          <div><dt className="text-[11px] font-bold text-[#637181]">Waiting</dt><dd className="mt-1 font-bold tabular-nums">{status.queue.pending}</dd></div>
          <div><dt className="text-[11px] font-bold text-[#637181]">Running</dt><dd className="mt-1 font-bold tabular-nums">{status.queue.running}</dd></div>
          <div><dt className="text-[11px] font-bold text-[#637181]">Failed jobs</dt><dd className="mt-1 font-bold tabular-nums">{status.queue.failed_jobs}</dd></div>
          <div><dt className="text-[11px] font-bold text-[#637181]">Oldest waiting</dt><dd className="mt-1 font-bold tabular-nums">{status.queue.oldest_pending_seconds === null ? "—" : `${Math.round(status.queue.oldest_pending_seconds / 60)} min`}</dd></div>
        </dl>
      </section>
      <p className="text-[11px] text-[#5f6c7b]">Database: {status.database.kind}{status.database.revision ? `, version ${status.database.revision}` : ""}. Invoice reader: {status.invoice_reader === "text_layer" ? "reads the PDF's own text on this machine" : "manual entry only"}. Steps for backups and upgrades are in docs/engineering/operations.md.</p>
    </>}
  </div>;
}
