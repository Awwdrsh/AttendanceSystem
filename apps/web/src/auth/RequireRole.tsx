import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { Center, Loader } from '@mantine/core';
import { useAuth } from './AuthProvider';
import type { Role } from '../api/types';
import { EmptyState } from '../components/EmptyState';

/** Route guard: signed-in, password changed, and (optionally) one of the roles. Admin passes every role check. */
export function RequireRole({ roles }: { roles?: Role[] }) {
  const { user, loading, hasRole } = useAuth();
  const loc = useLocation();
  if (loading) return <Center h="60vh"><Loader /></Center>;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  if (user.mustChangePassword && loc.pathname !== '/change-password') return <Navigate to="/change-password" replace />;
  if (roles && roles.length && !hasRole(...roles)) return <EmptyState title="Not available for your role" hint="Ask HR if you believe you need access to this page." />;
  return <Outlet />;
}
