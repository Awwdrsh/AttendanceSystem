import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { myCompanies } from '../api/group';
import { activeCompany, groupAccess, landingAfterSwitch, safeNext } from '../lib/group';
import { useAuth } from './AuthProvider';

const Ctx = createContext({
  me: null,
  loading: true,
  active: null,
  access: groupAccess(null),
  reload: async () => undefined,
  switchTo: async () => undefined,
});

/**
 * The companies the signed-in person can work in (GET /me/companies), their group roles and what the Group hub
 * offers them. Switching changes the session's active company on the server and reloads the app, so every page,
 * menu, feature switch and role check starts again from the new company.
 */
export function CompaniesProvider({ children }) {
  const { user } = useAuth();
  const [me, setMe] = useState(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    try {
      setMe(await myCompanies.list());
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!user) {
      setMe(null);
      setLoading(false);
      return;
    }
    void reload();
  }, [user?.id, user?.companyId, reload]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Work in another company. `next`: where to land there (a same-site path); default the current page, or its
   * hub root when the current address names a record of the old company, or home when the person's roles there do
   * not open it. */
  const switchTo = useCallback(async (companyId, next) => {
    const r = await myCompanies.switchTo(companyId);
    const target = next ? safeNext(next) : landingAfterSwitch(window.location.pathname, window.location.search, r?.roles);
    window.location.assign(target);
  }, []);

  const value = useMemo(
    () => ({ me, loading, active: activeCompany(me), access: groupAccess(me), reload, switchTo }),
    [me, loading, reload, switchTo],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useCompanies = () => useContext(Ctx);
