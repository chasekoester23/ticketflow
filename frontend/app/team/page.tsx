"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  addTeamMember, listAccountEvents, listTeam, resetTeamMemberPassword, updateTeamMember,
  type AccountEventRow, type IssuedPassword, type Role, type TeamMember,
} from "../../lib/api";
import { useAccount } from "../account-context";

const ROLE_OPTIONS: { value: Role; label: string; summary: string }[] = [
  { value: "clerk", label: "Clerk", summary: "Uploads scans, enters and corrects ticket fields, flags problems. Cannot approve quantities or change prices." },
  { value: "manager", label: "Manager", summary: "Everything a clerk does, plus approves tickets, skips non-tickets, sets rates and price overrides, and compares invoices." },
  { value: "viewer", label: "Viewer", summary: "Read-only access to records, dashboards and exports. Useful for accounting or executives." },
  { value: "owner", label: "Owner", summary: "Full access, including adding people, changing roles, and resetting passwords." },
];

const ACTION_LABEL: Record<string, string> = {
  member_added: "Added to the team",
  role_changed: "Role changed",
  deactivated: "Deactivated",
  reactivated: "Reactivated",
  password_reset: "Password reset by owner",
  password_changed: "Changed own password",
  display_name_changed: "Name changed",
  password_reset_terminal: "Password reset from server terminal",
  reactivated_terminal: "Reactivated from server terminal",
};

const roleLabel = (role: string | null) => ROLE_OPTIONS.find((r) => r.value === role)?.label ?? role ?? "";
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

export default function TeamPage() {
  const { can } = useAccount();
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [events, setEvents] = useState<AccountEventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<{ display_name: string; username: string; role: Role }>({ display_name: "", username: "", role: "clerk" });
  const [issued, setIssued] = useState<(IssuedPassword & { reason: "new" | "reset" }) | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState<{ id: number; action: "deactivate" | "reset" } | null>(null);
  const allowed = can("team.manage");

  const load = useCallback(async () => {
    try {
      const [m, e] = await Promise.all([listTeam(), listAccountEvents()]);
      setMembers(m); setEvents(e);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { if (allowed) void load(); }, [allowed, load]);

  async function act(fn: () => Promise<void>) {
    setError(null); setBusy(true);
    try { await fn(); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); setConfirming(null); }
  }

  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.username.trim() || busy) return;
    void act(async () => {
      const result = await addTeamMember({
        username: form.username.trim(), role: form.role,
        display_name: form.display_name.trim() || undefined,
      });
      setIssued({ ...result, reason: "new" }); setCopied(false);
      setForm({ display_name: "", username: "", role: "clerk" });
    });
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(true); }
    catch { setCopied(false); }
  }

  if (!allowed) {
    return <div className="tf-panel"><p className="tf-empty">Only company owners can manage the team. Ask an owner if you need a different role.</p></div>;
  }

  const activeCount = members.filter((m) => m.active).length;

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Team</h1>
        <p className="tf-page-description">Give each person their own sign-in. Approvals and corrections are recorded under the person who made them, so never share an account.</p></div>
      <span className="text-[12px] font-semibold text-[#5d6b7b]">{activeCount} active {activeCount === 1 ? "person" : "people"}</span>
    </div>

    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    {issued && <section role="status" className="rounded-md border border-[#f2cdb2] bg-[#fff8f2] p-5">
      <h2 className="text-[14px] font-bold text-[#7a3d17]">
        {issued.reason === "new" ? "Account created" : "Password reset"} for {issued.member.display_name || issued.member.username}
      </h2>
      <p className="mt-1 text-[12px] leading-6 text-[#7a4f33]">
        Give this temporary password to them in person or by phone. <strong>It is shown only once.</strong> They must choose their own password when they first sign in.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-[12px] text-[#6d5040]">Username <code className="ml-1 rounded bg-white px-2 py-1 font-mono text-[13px] text-[#1d2b38]">{issued.member.username}</code></span>
        <span className="text-[12px] text-[#6d5040]">Temporary password <code className="ml-1 select-all rounded bg-white px-2 py-1 font-mono text-[13px] text-[#1d2b38]">{issued.temporary_password}</code></span>
        <button type="button" className="tf-secondary-button" onClick={() => void copy(issued.temporary_password)}>{copied ? "Copied" : "Copy password"}</button>
        <button type="button" className="tf-link" onClick={() => setIssued(null)}>Done — hide it</button>
      </div>
    </section>}

    <section className="tf-panel">
      <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Add a person</h2>
        <p className="tf-panel-description">TicketFlow creates a one-time password for them. No email is sent.</p></div></div>
      <form className="grid items-end gap-4 p-5 md:grid-cols-2 xl:grid-cols-[1.2fr_1fr_170px_auto]" onSubmit={add}>
        <label className="tf-field-label">Full name
          <input className="tf-field w-full" placeholder="Maria Lopez" maxLength={100} value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        </label>
        <label className="tf-field-label">Username <span className="text-[10px] font-normal text-[#5f6c7b]">Used to sign in</span>
          <input className="tf-field w-full" required placeholder="mlopez" maxLength={100} autoCapitalize="none" autoCorrect="off" spellCheck={false}
            value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </label>
        <label className="tf-field-label">Role
          <select className="tf-field w-full" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            {ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </label>
        <button type="submit" className="tf-primary-button h-[38px]" disabled={busy || !form.username.trim()}>+ Add person</button>
        <p className="text-[12px] leading-5 text-[#5f6d7d] md:col-span-2 xl:col-span-4">
          <strong className="text-[#334253]">{roleLabel(form.role)}:</strong> {ROLE_OPTIONS.find((r) => r.value === form.role)?.summary}
        </p>
      </form>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading"><div><h2 className="tf-panel-title">People</h2>
        <p className="tf-panel-description">Role and access changes sign the person out immediately.</p></div></div>
      <div className="tf-table-wrap">
        <table className="tf-table">
          <thead><tr><th>Person</th><th>Role</th><th>Status</th><th className="text-right">Actions</th></tr></thead>
          <tbody>
            {members.map((m) => {
              const confirmingThis = confirming?.id === m.id ? confirming.action : null;
              return <tr key={m.id} className={m.active ? "" : "opacity-60"}>
                <td>
                  <div className="font-semibold text-[#1d2b38]">{m.display_name || m.username}{m.is_you && <span className="ml-2 text-[11px] font-normal text-[#5f6c7b]">(you)</span>}</div>
                  <div className="font-mono text-[11px] text-[#5f6c7b]">{m.username}</div>
                </td>
                <td>
                  {m.is_you
                    ? <span className={`tf-role-pill ${m.role === "owner" ? "tf-role-pill-owner" : ""}`}>{m.role_label}</span>
                    : <select aria-label={`Role for ${m.display_name || m.username}`} className="tf-field py-1.5" disabled={busy || !m.active}
                      value={m.role} onChange={(e) => void act(async () => { await updateTeamMember(m.id, { role: e.target.value as Role }); })}>
                      {ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                    </select>}
                </td>
                <td>
                  {!m.active ? <span className="tf-status">Deactivated</span>
                    : m.must_change_password ? <span className="tf-status tf-status-attention">Awaiting first sign-in</span>
                    : <span className="tf-status tf-status-open">Active</span>}
                </td>
                <td className="text-right">
                  {m.is_you ? <Link prefetch={false} href="/account" className="tf-link">Account settings</Link>
                    : confirmingThis ? <span className="inline-flex flex-wrap items-center justify-end gap-2 text-[12px] text-[#5d6b7b]">
                      {confirmingThis === "deactivate" ? "Sign them out and block access?" : "Issue a new temporary password?"}
                      <button type="button" className="tf-secondary-button min-h-[30px] py-1" disabled={busy}
                        onClick={() => void act(async () => {
                          if (confirmingThis === "deactivate") {
                            await updateTeamMember(m.id, { active: false });
                          } else {
                            const result = await resetTeamMemberPassword(m.id);
                            setIssued({ ...result, reason: "reset" }); setCopied(false);
                          }
                        })}>{confirmingThis === "deactivate" ? "Deactivate" : "Reset password"}</button>
                      <button type="button" className="tf-link" onClick={() => setConfirming(null)}>Cancel</button>
                    </span>
                    : <span className="inline-flex flex-wrap justify-end gap-3">
                      {m.active && <button type="button" className="tf-link" disabled={busy} onClick={() => setConfirming({ id: m.id, action: "reset" })}>Reset password</button>}
                      {m.active
                        ? <button type="button" className="tf-link text-[#a73631]" disabled={busy} onClick={() => setConfirming({ id: m.id, action: "deactivate" })}>Deactivate</button>
                        : <button type="button" className="tf-link" disabled={busy} onClick={() => void act(async () => { await updateTeamMember(m.id, { active: true }); })}>Reactivate</button>}
                    </span>}
                </td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Recent account activity</h2>
        <p className="tf-panel-description">A permanent record of team changes in your company.</p></div></div>
      {events.length === 0 ? <p className="tf-empty">No team changes recorded yet.</p> :
        <ul className="divide-y divide-[#edf0f3]">
          {events.slice(0, 40).map((e) => <li key={e.id} className="flex flex-wrap items-baseline justify-between gap-2 px-5 py-3 text-[12px]">
            <span className="text-[#334253]">
              <strong>{e.target ?? "Unknown person"}</strong> — {ACTION_LABEL[e.action] ?? e.action}
              {e.action === "role_changed" && <> ({roleLabel(e.old)} → {roleLabel(e.new)})</>}
              {e.action === "member_added" && e.new && <> as {roleLabel(e.new)}</>}
            </span>
            <span className="text-[#5f6c7b]">{e.actor_kind === "terminal" ? "server administrator" : e.actor ?? "—"} · {when(e.at)}</span>
          </li>)}
        </ul>}
    </section>
  </div>;
}
