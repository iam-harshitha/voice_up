import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, setCsrfToken, setUnauthorizedHandler } from "../api/client";
import { api } from "../api/endpoints";
import type { SessionInfo } from "../api/types";

type AuthState =
  | { status: "loading" }
  | { status: "signed_out"; reason?: string }
  | { status: "signed_in"; session: SessionInfo };

interface AuthContextValue {
  state: AuthState;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (scope: string) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  const signedOut = useCallback((reason?: string) => {
    setCsrfToken(null);
    setState({ status: "signed_out", reason });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    api
      .session({ signal: controller.signal })
      .then((session) => {
        setCsrfToken(session.csrf_token);
        setState({ status: "signed_in", session });
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException) return;
        signedOut();
      });
    return () => controller.abort();
  }, [signedOut]);

  useEffect(() => {
    setUnauthorizedHandler(() => signedOut("Your session has ended. Please sign in again."));
    return () => setUnauthorizedHandler(null);
  }, [signedOut]);

  const login = useCallback(async (email: string, password: string) => {
    const session = await api.login(email, password);
    setCsrfToken(session.csrf_token);
    setState({ status: "signed_in", session });
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    } finally {
      signedOut();
    }
  }, [signedOut]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      login,
      logout,
      can: (scope) => state.status === "signed_in" && state.session.scopes.includes(scope),
    }),
    [state, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export function useSession(): SessionInfo {
  const { state } = useAuth();
  if (state.status !== "signed_in") throw new Error("useSession requires a signed-in user");
  return state.session;
}
