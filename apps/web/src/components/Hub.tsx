import { Tabs } from '@mantine/core';
import type { ReactNode } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import type { Role } from '../api/types';
import { EmbeddedContext } from './embedded';
import { PageHeader } from './PageHeader';


export interface HubTab { path: string; label: string; element: ReactNode; roles?: Role[]; show?: boolean }

/** One page with a title and tabs; each tab has its own URL so links and the back button work. */
export function Hub({ title, subtitle, tabs }: { title: string; subtitle?: string; tabs: HubTab[] }) {
  const { hasRole } = useAuth();
  const loc = useLocation();
  const nav = useNavigate();
  const visible = tabs.filter((t) => t.show !== false && (!t.roles || hasRole(...t.roles)));
  const path = loc.pathname.replace(/\/$/, '');
  const active = visible.find((t) => t.path === path);
  if (!active) return visible[0] ? <Navigate to={visible[0].path} replace /> : <Navigate to="/" replace />;
  return (
    <>
      <PageHeader title={title} {...(subtitle ? { subtitle } : {})} />
      {visible.length > 1 ? (
        <Tabs value={active.path} onChange={(v) => v && nav(v)} mb="lg" keepMounted={false}>
          <Tabs.List style={{ flexWrap: 'nowrap', overflowX: 'auto', overflowY: 'hidden' }}>
            {visible.map((t) => <Tabs.Tab key={t.path} value={t.path} style={{ whiteSpace: 'nowrap' }}>{t.label}</Tabs.Tab>)}
          </Tabs.List>
        </Tabs>
      ) : null}
      <EmbeddedContext.Provider value><div className="hj-embedded">{active.element}</div></EmbeddedContext.Provider>
    </>
  );
}
