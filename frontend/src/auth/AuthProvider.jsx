import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { auth as authApi } from '../api';
import { onPasswordChangeRequired, onTwoFactorSetupRequired, onUnauthorized } from '../api/client';
import { security } from '../api/security';
/** The password was right and two-step sign-in is on: the code from the app completes the sign-in. */
const Ctx = createContext({
  user: null,
  loading: true,
  login: async () => {
    throw new Error('no provider');
  },
  completeTwoFactor: async () => {
    throw new Error('no provider');
  },
  logout: async () => undefined,
  refresh: async () => undefined,
  hasRole: () => false,
});

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const r = await authApi.me();
      setUser(r.user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => onUnauthorized(() => setUser(null)), []);
  useEffect(() => onPasswordChangeRequired(() => setUser((u) => (u ? { ...u, mustChangePassword: true } : u))), []);
  useEffect(() => onTwoFactorSetupRequired(() => setUser((u) => (u ? { ...u, mustSetUpTwoFactor: true } : u))), []);

  const value = useMemo(
    () => ({
      user,
      loading,
      refresh,
      login: async (email, password) => {
        const r = await security.login(email, password);
        if ('twoFactorRequired' in r) return r;
        setUser(r.user);
        return r.user;
      },
      completeTwoFactor: async (challenge, code) => {
        const r = await security.loginTwoFactor(challenge, code);
        setUser(r.user);
        return { user: r.user, usedRecoveryCode: !!r.usedRecoveryCode };
      },
      logout: async () => {
        try {
          await authApi.logout();
        } finally {
          setUser(null);
        }
      },
      hasRole: (...roles) => !!user && (user.roles.includes('admin') || roles.some((r) => user.roles.includes(r))),
    }),
    [user, loading, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useAuth = () => useContext(Ctx);
