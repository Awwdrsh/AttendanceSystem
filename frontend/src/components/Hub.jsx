import { Tabs } from '@mantine/core';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { useFeatures } from '../lib/features';
import { EmbeddedContext } from './embedded';
import { PageHeader } from './PageHeader';

/** `feature` hides the tab while that module is switched off under Settings → Features. */
/** One page with a title and tabs; each tab has its own URL so links and the back button work. */
export function Hub({ title, subtitle, tabs }) {
  const { hasRole } = useAuth();
  const { on } = useFeatures();
  const loc = useLocation();
  const nav = useNavigate();
  const visible = tabs.filter((t) => t.show !== false && on(t.feature) && (!t.roles || hasRole(...t.roles)));
  const path = loc.pathname.replace(/\/$/, '');
  const active = visible.find((t) => t.path === path);
  if (!active) return visible[0] ? <Navigate to={visible[0].path} replace /> : <Navigate to="/" replace />;
  return (
    <>
      <PageHeader title={title} {...(subtitle ? { subtitle } : {})} />
      {visible.length > 1 ? (
        <Tabs value={active.path} onChange={(v) => v && nav(v)} mb="lg" keepMounted={false}>
          <Tabs.List style={{ flexWrap: 'nowrap', overflowX: 'auto', overflowY: 'hidden' }}>
            {visible.map((t) => (
              <Tabs.Tab key={t.path} value={t.path} style={{ whiteSpace: 'nowrap' }}>
                {t.label}
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs>
      ) : null}
      <EmbeddedContext.Provider value>
        <div className="hj-embedded">{active.element}</div>
      </EmbeddedContext.Provider>
    </>
  );
}
