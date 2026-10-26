import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button, Field, inputClass } from "../components/ui";

export function LoginPage({ notice }: { notice?: string }) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.status === 401
            ? caught.message
            : caught.userMessage
          : "Sign-in failed unexpectedly.",
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold text-slate-900">Approval Line</h1>
        <p className="mb-5 text-sm text-slate-500">Sign in to the operator dashboard.</p>
        {notice && (
          <p role="status" className="mb-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {notice}
          </p>
        )}
        <form onSubmit={submit} className="space-y-4" noValidate>
          <Field label="Email">
            {(props) => (
              <input
                {...props}
                type="email"
                autoComplete="username"
                required
                maxLength={320}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputClass}
              />
            )}
          </Field>
          <Field label="Password">
            {(props) => (
              <input
                {...props}
                type="password"
                autoComplete="current-password"
                required
                maxLength={256}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={inputClass}
              />
            )}
          </Field>
          {error && (
            <p role="alert" className="text-sm font-medium text-red-700">
              {error}
            </p>
          )}
          <Button type="submit" variant="primary" className="w-full" disabled={busy || !email || !password}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        <p className="mt-4 text-xs text-slate-500">
          Accounts are created by an operator with <code>python -m app.cli create-dashboard-user</code>.
        </p>
      </div>
    </main>
  );
}
