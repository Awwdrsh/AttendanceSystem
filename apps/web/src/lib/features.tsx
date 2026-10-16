import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { features as featuresApi, type FeatureKey } from '../api/catalog';
import { useAuth } from '../auth/AuthProvider';

interface FeaturesState { on: (key: FeatureKey | undefined) => boolean; loaded: boolean; reload: () => Promise<void> }

const FeaturesContext = createContext<FeaturesState>({ on: () => true, loaded: false, reload: async () => undefined });

/**
 * The company's module switches (Settings → Features). Until they load, and if they cannot load, everything counts as
 * on: the server refuses switched-off modules anyway, so the worst case is a menu item that leads to an empty page.
 */
export function FeaturesProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const [enabled, setEnabled] = useState<Partial<Record<FeatureKey, boolean>>>({});
  const [loaded, setLoaded] = useState(false);
  const reload = useCallback(async () => {
    try { setEnabled((await featuresApi.get()).enabled); } catch { /* keep the last known switches */ } finally { setLoaded(true); }
  }, []);
  useEffect(() => { if (user) void reload(); }, [user?.id, reload]); // eslint-disable-line react-hooks/exhaustive-deps
  const value = useMemo<FeaturesState>(() => ({ on: (k) => !k || enabled[k] !== false, loaded, reload }), [enabled, loaded, reload]);
  return <FeaturesContext.Provider value={value}>{children}</FeaturesContext.Provider>;
}

export const useFeatures = () => useContext(FeaturesContext);

/** A page that belongs to a module: when the module is switched off, go home instead. */
export function FeatureRoute({ feature, children }: { feature: FeatureKey; children: ReactNode }) {
  const { on, loaded } = useFeatures();
  if (loaded && !on(feature)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** Render children only while the module is on (Home cards, buttons). */
export function IfFeature({ feature, children }: { feature: FeatureKey; children: ReactNode }) {
  const { on } = useFeatures();
  return on(feature) ? <>{children}</> : null;
}
