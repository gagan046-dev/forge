import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { setUnauthorizedHandler } from "./api/client";
import { auth } from "./api/endpoints";

type AuthState = "checking" | "signed-in" | "signed-out";

interface AuthContextValue {
  state: AuthState;
  /** Set when /api/auth/me itself failed (gateway down), so the login page can say so. */
  checkError: unknown;
  markSignedIn: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>("checking");
  const [checkError, setCheckError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    auth
      .me()
      .then((me) => !cancelled && setState(me.authenticated ? "signed-in" : "signed-out"))
      .catch((err: unknown) => {
        if (cancelled) return;
        setCheckError(err);
        setState("signed-out");
      });
    // Any /api 401 (expired 12 h session) drops the user back to the login page.
    setUnauthorizedHandler(() => setState("signed-out"));
    return () => {
      cancelled = true;
      setUnauthorizedHandler(null);
    };
  }, []);

  const markSignedIn = useCallback(() => {
    setCheckError(null);
    setState("signed-in");
  }, []);

  const signOut = useCallback(async () => {
    await auth.logout();
    setState("signed-out");
  }, []);

  const value = useMemo(() => ({ state, checkError, markSignedIn, signOut }), [state, checkError, markSignedIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
