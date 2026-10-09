import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useId, useState } from "react";
import { api, ApiError, setCsrfToken } from "../api/client";
import { fetchers, keys } from "../api/queries";
import type { LoginResponse } from "../api/types";
import { Button, Segmented } from "../components/Button";
import { Field, PasswordInput, TextInput } from "../components/Form";
import { Notice } from "../components/Notice";
import { useTitle } from "../lib/hooks";
import { safeNext, useSearchParams, useSetParams } from "../lib/url";

function loginError(err: unknown, method: "password" | "key"): string {
  if (err instanceof ApiError) {
    if (err.isNetwork) return `Could not reach the daemon at ${window.location.host}. Check that it is running with eks-harness daemon status.`;
    if (err.status === 429) return "Too many attempts. Try again in 1 min.";
    if (err.error === "user_disabled" || /disabled/i.test(err.message)) return "This account is disabled. Ask an admin to enable it.";
    if (err.status === 401 || err.status === 403 || err.status === 400) {
      return method === "key" ? "This API key is not valid. It may have been revoked." : "Username or password is wrong.";
    }
    return `Could not sign in: ${err.message.replace(/\.$/, "")} (${err.status}).`;
  }
  return "Could not sign in.";
}

export function LoginPage() {
  useTitle("Sign in");
  const params = useSearchParams();
  const setParams = useSetParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const method = params.method === "key" ? "key" : "password";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const userId = useId();
  const passId = useId();
  const keyId = useId();
  const next = safeNext(params.next);

  useEffect(() => {
    let cancelled = false;
    fetchers
      .me()
      .then((me) => {
        if (cancelled) return;
        if (!me.authEnabled || me.via !== "local") {
          setCsrfToken(me.csrfToken ?? null);
          client.setQueryData(keys.me, me);
          void navigate({ to: next, replace: true });
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [client, navigate, next]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const body = method === "key" ? { apiKey: apiKey.trim() } : { username: username.trim(), password };
      const response = await api.post<LoginResponse>("/api/auth/login", body, { handleUnauthorized: false });
      setCsrfToken(response.csrfToken);
      client.clear();
      window.location.assign(next);
    } catch (err) {
      setBusy(false);
      setError(loginError(err, method));
    }
  };

  const notice = params.notice === "revoked" ? "Your key was revoked. Sign in again." : params.notice === "expired" ?"Your session expired. Sign in again." : null;

  return (
    <div className="login">
      <main id="main" className="login-main">
        <p className="login-product">eks-harness</p>
        <h1 className="login-title" tabIndex={-1} data-page-title="">
          Sign in
        </h1>
        <form className="login-form" onSubmit={submit} noValidate>
          {notice ? <Notice variant="attention">{notice}</Notice> : null}
          <Segmented
            label="Sign-in method"
            value={method}
            onChange={(value) => {
              setError(null);
              setParams({ method: value === "key" ? "key" : null }, { replace: true });
            }}
            options={[
              { value: "password", label: "Password" },
              { value: "key", label: "API key" },
            ]}
          />
          {method === "password" ? (
            <>
              <Field label="Username" htmlFor={userId}>
                <TextInput id={userId} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} autoFocus />
              </Field>
              <Field label="Password" htmlFor={passId}>
                <PasswordInput id={passId} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
              </Field>
            </>
          ) : (
            <Field label="API key" htmlFor={keyId} helper="Paste a key created with eks-harness keys create or in Settings > API keys.">
              <PasswordInput id={keyId} mono value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={"ehk_…"} autoComplete="off" spellCheck={false} autoFocus />
            </Field>
          )}
          {error ? <Notice variant="error">{error}</Notice> : null}
          <div>
            <Button type="submit" variant="primary" busy={busy} busyLabel={"Signing in…"} className="login-submit">
              Sign in
            </Button>
          </div>
          <p className="login-help">
            Lost access? On the machine running the daemon, run:
            <span className="mono login-command">eks-harness auth recover</span>
          </p>
        </form>
      </main>
    </div>
  );
}
