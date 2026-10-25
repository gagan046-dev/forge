import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, describeError } from "../api/client";
import { auth } from "../api/endpoints";
import { useAuth } from "../auth";

function safeNext(next: string | null): string {
  // Only same-app paths, so ?next= can't bounce the user to another site.
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/chat";
}

export function LoginPage() {
  const { state, checkError, markSignedIn } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const next = safeNext(params.get("next"));

  if (state === "signed-in") return <Navigate to={next} replace />;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.login(password);
      markSignedIn();
      navigate(next, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setError("Wrong password.");
      else if (err instanceof ApiError && err.status === 429)
        setError(`Too many attempts. Wait ${err.retryAfter ?? 60}s and try again.`);
      else setError(describeError(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login-box" onSubmit={(e) => void onSubmit(e)}>
        <div className="brand brand-login">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">forge</span>
        </div>
        <label className="field">
          <span className="field-label">Admin password</span>
          <input
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}
        {!error && checkError !== null && (
          <div className="banner banner-error" role="alert">
            {describeError(checkError)}
          </div>
        )}
        <button type="submit" className="btn btn-primary btn-block" disabled={busy || password.length === 0}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="login-hint">
          <code>scripts/seal-secrets.ps1</code> generates the password and keeps it in the git-ignored{" "}
          <code>forge/secrets/</code> folder on the machine that ran <code>scripts/up.ps1</code>.
        </p>
      </form>
    </div>
  );
}
