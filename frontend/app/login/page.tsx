"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { getSetupStatus, logIn } from "../../lib/api";
import { PasswordField, PublicFrame, SubmitButton, TextField } from "../public-ui";

const LOGIN_NOTICE_KEY = "tf-login-notice";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [errors, setErrors] = useState({ username: "", password: "" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // A server with no accounts has nobody to sign in. One that can't answer gets the usual form.
    getSetupStatus().then((status) => { if (status.needed) router.replace("/signup"); }).catch(() => {});
    try {
      const message = sessionStorage.getItem(LOGIN_NOTICE_KEY);
      if (message) {
        setNotice(message);
        sessionStorage.removeItem(LOGIN_NOTICE_KEY);
      }
    } catch { /* Storage can be blocked. The notice is only a courtesy. */ }
  }, [router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const problems = { username: username.trim() ? "" : "Enter your username.", password: password ? "" : "Enter your password." };
    setErrors(problems);
    setError("");
    if (problems.username || problems.password) {
      (problems.username ? usernameRef : passwordRef).current?.focus();
      return;
    }
    setPending(true);
    try {
      const account = await logIn(username, password);
      window.location.replace(account.must_change_password ? "/account" : "/");
    } catch (err) {
      setError((err as Error).message);
      setPending(false);
      passwordRef.current?.focus();
    }
  }

  return <PublicFrame aside={<>
    <p className="tf-pub-kicker">Material delivery control</p>
    <p className="tf-pub-statement">Every load.<br />Every ticket.<br />Accounted for.</p>
    <p className="tf-pub-aside-note">Everyone signs in with their own account, so every approval carries a name.</p>
  </>}>
    <h1 className="tf-pub-title">Sign in to TicketFlow</h1>
    <p className="tf-pub-lead">Use the username and password from your company&apos;s TicketFlow owner.</p>
    {notice && <div role="status" className="tf-pub-banner tf-pub-banner-notice">{notice}</div>}
    {error && <div role="alert" className="tf-pub-banner tf-pub-banner-error">{error}</div>}
    <form className="tf-pub-form" onSubmit={submit} noValidate>
      <TextField id="username" name="username" label="Username" autoComplete="username" autoCapitalize="none" spellCheck={false}
        value={username} onChange={setUsername} error={errors.username} inputRef={usernameRef} />
      <PasswordField id="password" name="password" label="Password" autoComplete="current-password" warnCapsLock
        value={password} onChange={setPassword} error={errors.password} inputRef={passwordRef}
        visible={showPassword} onToggle={() => setShowPassword((old) => !old)} />
      <SubmitButton pending={pending} pendingText="Signing in…">Sign in</SubmitButton>
    </form>
    <p className="tf-pub-footnote">Forgot your password? An owner at your company can reset it from the Team page.</p>
    <p className="tf-pub-switch">New to TicketFlow? <Link href="/signup">Request access</Link></p>
  </PublicFrame>;
}
