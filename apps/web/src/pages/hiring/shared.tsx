import { Badge, Tooltip } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { hiring, type Lookups } from '../../api/hiring';
import { fmtDateShort, fmtTime } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export const ICON = { size: 16, stroke: 1.6 } as const;

/** Nepal calendar date (YYYY-MM-DD) of an instant. */
export function nptDateOf(iso: string): string {
  return new Date(Date.parse(iso) + 345 * 60_000).toISOString().slice(0, 10);
}

/** "27 Ashwin, 11:00" for an instant, in Nepal time and the BS calendar. */
export function fmtWhen(iso: string): string {
  return `${fmtDateShort(nptDateOf(iso))}, ${fmtTime(iso)}`;
}

const EMPTY: Lookups = { users: [], orgUnits: [], branches: [], shifts: [] };

export function useLookups(): Lookups {
  const q = useQuery(() => hiring.lookups(), []);
  return q.data ?? EMPTY;
}

const JOB_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: 'Draft', color: 'gray' }, open: { label: 'Open', color: 'teal' }, closed: { label: 'Closed', color: 'dark' },
};
export function JobStatusBadge({ status }: { status: string }) {
  const s = JOB_STATUS[status] ?? { label: status, color: 'gray' };
  return <Badge variant="light" color={s.color} size="sm">{s.label}</Badge>;
}

const REQ_STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: 'Waiting for HR', color: 'yellow' }, approved: { label: 'Approved', color: 'teal' }, rejected: { label: 'Rejected', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' }, filled: { label: 'Filled', color: 'brand' },
};
export function RequisitionStatusBadge({ status }: { status: string }) {
  const s = REQ_STATUS[status] ?? { label: status, color: 'gray' };
  return <Badge variant="light" color={s.color} size="sm">{s.label}</Badge>;
}

const APP_STATUS: Record<string, { label: string; color: string }> = {
  active: { label: 'In progress', color: 'blue' }, rejected: { label: 'Not selected', color: 'gray' }, withdrawn: { label: 'Withdrawn', color: 'gray' }, hired: { label: 'Hired', color: 'teal' },
};
export function ApplicationStatusBadge({ status }: { status: string }) {
  const s = APP_STATUS[status] ?? { label: status, color: 'gray' };
  return <Badge variant="light" color={s.color} size="sm">{s.label}</Badge>;
}

const OFFER_STATUS: Record<string, { label: string; color: string }> = {
  pending_approval: { label: 'Waiting for approval', color: 'yellow' }, approved: { label: 'Approved, not answered', color: 'blue' }, rejected: { label: 'Not approved', color: 'red' },
  accepted: { label: 'Accepted', color: 'teal' }, declined: { label: 'Declined', color: 'orange' }, withdrawn: { label: 'Withdrawn', color: 'gray' },
};
export function OfferStatusBadge({ status }: { status: string }) {
  const s = OFFER_STATUS[status] ?? { label: status, color: 'gray' };
  return <Badge variant="light" color={s.color} size="sm">{s.label}</Badge>;
}

/** Screening score with the must-have warning; the drawer explains every point. */
export function ScoreBadge({ score, knockedOut }: { score: number | null; knockedOut: boolean }) {
  if (knockedOut) {
    return (
      <Tooltip label="Did not meet a must-have question" withArrow>
        <Badge variant="light" color="red" size="sm" leftSection={<IconAlertTriangle size={11} stroke={1.8} />}>{score ?? '—'}</Badge>
      </Tooltip>
    );
  }
  if (score === null) return <Badge variant="light" color="gray" size="sm">No score</Badge>;
  return <Badge variant="light" color={score >= 75 ? 'teal' : score >= 50 ? 'yellow' : 'gray'} size="sm">{score}</Badge>;
}

/** Open an API-served HTML page or file (offer letter, CV) in a new tab with the session cookie. */
export function openInTab(url: string): void {
  window.open(url, '_blank', 'noopener');
}
