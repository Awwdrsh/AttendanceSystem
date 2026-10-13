import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { auth as authApi } from '../api';
import { onPasswordChangeRequired, onUnauthorized } from '../api/client';
import type { AuthUser, Role } from '../api/types';

export interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<AuthUser>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  hasRole: (...roles: Role[]) => boolean;
}

const Ctx = createContext<AuthState>({ user: null, loading: true, login: async () => { throw new Error('no provider'); }, logout: async () => undefined, refresh: async () => undefined, hasRole: () => false });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try { const r = await authApi.me(); setUser(r.user); } catch { setUser(null); } finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => onUnauthorized(() => setUser(null)), []);
  useEffect(() => onPasswordChangeRequired(() => setUser((u) => (u ? { ...u, mustChangePassword: true } : u))), []);

  const value = useMemo<AuthState>(() => ({
    user, loading, refresh,
    login: async (email, password) => { const r = await authApi.login(email, password); setUser(r.user); return r.user; },
    logout: async () => { try { await authApi.logout(); } finally { setUser(null); } },
    hasRole: (...roles) => !!user && (user.roles.includes('admin') || roles.some((r) => user.roles.includes(r))),
  }), [user, loading, refresh]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useAuth = (): AuthState => useContext(Ctx);
