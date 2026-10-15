import { Avatar, Badge, Button, Divider, Group, Modal, Paper, SegmentedControl, Skeleton, Stack, Text, Textarea, Timeline } from '@mantine/core';
import { IconCheck, IconChecks, IconCircleCheck, IconX } from '@tabler/icons-react';
import { Fragment, useState, type ReactNode } from 'react';
import { workflow as wfApi } from '../../api';
import type { InboxItem, WorkflowDetail } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { bsMonthLabel, fmtDateShort, fmtMoney, fmtTime, minutesToHm, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const TYPE_LABEL: Record<string, string> = { leave_request: 'Leave', regularisation_request: 'Attendance correction', ot_request: 'Overtime', payroll_run: 'Payroll run', letter_request: 'Letter request' };
const TYPE_COLOR: Record<string, string> = { leave_request: 'blue', regularisation_request: 'orange', ot_request: 'grape', payroll_run: 'brand', letter_request: 'teal' };

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
}

function dateRange(from: unknown, to: unknown): string {
  const f = String(from); const t = String(to);
  return t && t !== 'undefined' && t !== f ? `${fmtDateShort(f)} to ${fmtDateShort(t)}` : fmtDateShort(f);
}

/** One readable line of what is being asked, plus an optional muted line of context. */
function Summary({ item }: { item: InboxItem }) {
  const s = item.summary as Record<string, unknown>;
  let main: ReactNode; const extra: ReactNode[] = [];
  switch (item.objectType) {
    case 'leave_request': {
      const days = Number(s.days);
      main = <>{String(s.leaveTypeName ?? s.leaveType)}, {dateRange(s.from, s.to)}{s.halfDay ? ' (half day)' : ''} <Text span c="dimmed">({String(s.days)} {days === 1 ? 'day' : 'days'})</Text></>;
      extra.push(<Text key="bal" size="xs" c="dimmed">Balance {Number(s.balanceNow ?? 0).toFixed(1)} to {Number(s.balanceAfter ?? 0).toFixed(1)}</Text>);
      if (Array.isArray(s.teamOverlap) && s.teamOverlap.length) extra.push(<Text key="ov" c="orange.7" size="xs">Also off: {(s.teamOverlap as string[]).join(', ')}</Text>);
      if (s.reason) extra.push(<Text key="r" size="xs" c="dimmed">“{String(s.reason)}”</Text>);
      break;
    }
    case 'regularisation_request': {
      const KIND: Record<string, string> = { wfh: 'Working from home', missed_punch: 'Missed punch', on_duty: 'On duty elsewhere', half_day: 'Half day', shift_change: 'Shift change' };
      const kind = KIND[String(s.kind)] ?? String(s.kind).replace(/_/g, ' ');
      main = <>{kind.charAt(0).toUpperCase() + kind.slice(1)} on {fmtDateShort(String(s.local_date))}{s.punch_time ? `, ${String(s.punch_direction ?? '').toLowerCase() === 'in' ? 'clock-in' : 'clock-out'} at ${fmtTime(String(s.punch_time))}` : ''}</>;
      if (s.reason) extra.push(<Text key="r" size="xs" c="dimmed">“{String(s.reason)}”</Text>);
      break;
    }
    case 'ot_request':
      main = <>{minutesToHm(Number(s.requested_min))} overtime on {fmtDateShort(String(s.local_date))}</>;
      if (s.reason) extra.push(<Text key="r" size="xs" c="dimmed">“{String(s.reason)}”</Text>);
      break;
    case 'payroll_run':
      main = <>{String(s.run_type).charAt(0).toUpperCase() + String(s.run_type).slice(1)} run for {bsMonthLabel(Number(s.bs_year), Number(s.bs_month))}</>;
      extra.push(<Text key="p" size="xs" c="dimmed">{String(s.payslips)} payslips</Text>, <Text key="n" size="xs" c="dimmed">Net {fmtMoney(String(s.total_net))}</Text>);
      break;
    case 'letter_request':
      main = <>{String(s.letter ?? 'Letter')} requested</>;
      if (s.purpose) extra.push(<Text key="p" size="xs" c="dimmed">For {String(s.purpose)}</Text>);
      if (s.addressed_to) extra.push(<Text key="a" size="xs" c="dimmed">To {String(s.addressed_to)}</Text>);
      break;
    default:
      main = <Text span c="dimmed">{JSON.stringify(s)}</Text>;
  }
  return (
    <>
      <Text size="sm">{main}</Text>
      {extra.length ? <Group gap="md" wrap="wrap">{extra}</Group> : null}
    </>
  );
}

export default function ApprovalsPage() {
  const q = useQuery(() => wfApi.inbox(), []);
  const [reject, setReject] = useState<InboxItem | null>(null); const [reason, setReason] = useState('');
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);
  const [kind, setKind] = useState('all');
  const items = q.data?.items ?? [];
  const kinds = [...new Set(items.map((i) => i.objectType))];
  const shown = kind === 'all' ? items : items.filter((i) => i.objectType === kind);

  const approve = async (it: InboxItem) => { setBusy(it.instanceId); try { await wfApi.approve(it.instanceId); toast.ok('Approved.'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const doReject = async () => { if (!reject) return; setBusy(reject.instanceId); try { await wfApi.reject(reject.instanceId, reason); toast.ok('Rejected.'); setReject(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const bulk = async () => { setBusy('bulk'); try { const r = await wfApi.bulkApprove(items.map((i) => i.instanceId)); const ok = r.results.filter((x) => x.state).length; toast.ok(`${ok} approved${r.results.length - ok ? `, ${r.results.length - ok} failed` : ''}.`); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };

  return (
    <Stack gap="md">
      <PageHeader title="Approvals"
        subtitle={items.length ? `${items.length} ${items.length === 1 ? 'request is' : 'requests are'} waiting for your decision.` : 'Leave, attendance correction and overtime requests that need your decision.'}
        actions={items.length > 1 ? <Button variant="default" leftSection={<IconChecks size={16} stroke={1.6} />} loading={busy === 'bulk'} onClick={() => setConfirmBulk(true)}>Approve all ({items.length})</Button> : null} />

      {kinds.length > 1 ? (
        <SegmentedControl value={kind} onChange={setKind} style={{ alignSelf: 'flex-start' }}
          data={[{ value: 'all', label: `All (${items.length})` }, ...kinds.map((k) => ({ value: k, label: `${TYPE_LABEL[k] ?? k} (${items.filter((i) => i.objectType === k).length})` }))]} />
      ) : null}

      {q.error ? <EmptyState title="Could not load the inbox" hint={`${q.error}. Refresh the page to try again.`} /> : null}
      {q.loading && items.length === 0 ? <Paper withBorder p="md"><Stack>{[0, 1, 2].map((i) => <Skeleton key={i} h={48} />)}</Stack></Paper> : null}
      {!q.loading && !q.error && items.length === 0 ? <EmptyState title="You're all caught up" hint="New leave, attendance correction and overtime requests from your team will show up here." icon={<IconCircleCheck size={20} stroke={1.6} />} /> : null}

      {shown.length ? (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          {shown.map((it, idx) => {
            const name = it.employee?.nameEn ?? 'Unknown employee';
            return (
              <Fragment key={it.instanceId}>
                {idx > 0 ? <Divider color="var(--hj-line-soft)" /> : null}
                <Group px="md" py="sm" gap="md" wrap="wrap" align="flex-start">
                  <Avatar radius="xl" size={36} color="brand" variant="light">{initials(name)}</Avatar>
                  <Stack gap={3} style={{ flex: 1, minWidth: 220 }}>
                    <Group gap={8} wrap="wrap">
                      <Text size="sm" fw={500}>{name}</Text>
                      {it.employee ? <Text size="xs" c="dimmed">{it.employee.empCode}</Text> : null}
                      {(it.summary as Record<string, unknown>)?.kind === 'wfh'
                        ? <Badge variant="light" color="blue" size="sm">Work from home</Badge>
                        : <Badge variant="light" color={TYPE_COLOR[it.objectType] ?? 'gray'} size="sm">{TYPE_LABEL[it.objectType] ?? it.objectType}</Badge>}
                    </Group>
                    <Summary item={it} />
                  </Stack>
                  <Stack gap={8} align="flex-end" style={{ marginLeft: 'auto' }}>
                    <Text size="xs" c="dimmed" ta="right" title={new Date(it.createdAt).toLocaleString('en-GB')}>
                      {relativeTime(it.createdAt)}{it.totalSteps > 1 ? `, step ${it.step + 1} of ${it.totalSteps}` : ''}
                    </Text>
                    <Group gap={6} wrap="nowrap">
                      <Button variant="subtle" color="gray" size="xs" onClick={async () => { try { setDetail(await wfApi.detail(it.instanceId)); } catch (e) { toast.err(e); } }}>Details</Button>
                      <Button variant="default" size="xs" leftSection={<IconX size={14} stroke={1.8} />} disabled={busy === it.instanceId} onClick={() => setReject(it)}>Reject</Button>
                      <Button size="xs" leftSection={<IconCheck size={14} stroke={1.8} />} loading={busy === it.instanceId} onClick={() => approve(it)}>Approve</Button>
                    </Group>
                  </Stack>
                </Group>
              </Fragment>
            );
          })}
        </Paper>
      ) : null}

      <Modal opened={!!reject} onClose={() => setReject(null)} title="Reject request" centered>
        <Stack>
          {reject ? <Text size="sm" c="dimmed">{reject.employee?.nameEn ?? 'This employee'} will see your reason.</Text> : null}
          <Textarea label="Reason" description="At least 3 characters." value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} required data-autofocus />
          <Group justify="flex-end"><Button variant="default" onClick={() => setReject(null)}>Cancel</Button><Button color="red" leftSection={<IconX size={16} stroke={1.6} />} loading={!!reject && busy === reject.instanceId} disabled={reason.trim().length < 3} onClick={doReject}>Reject request</Button></Group>
        </Stack>
      </Modal>

      <ConfirmDialog opened={confirmBulk} onClose={() => setConfirmBulk(false)} color="brand" title="Approve all requests?" confirmLabel={`Approve ${items.length}`}
        message={`${items.length} requests will be approved in one go. Reject any that need it first.`} onConfirm={bulk} />

      <Modal opened={!!detail} onClose={() => setDetail(null)} title="Approval history" centered>
        {detail ? (
          <Stack>
            <Group gap="xs"><Text size="sm" c="dimmed">Approval chain:</Text><Text size="sm">{detail.instance.steps.map((s) => s.role).join(', then ')}</Text></Group>
            <Group gap="xs"><Text size="sm" c="dimmed">State:</Text><Text size="sm">{detail.instance.state.charAt(0).toUpperCase() + detail.instance.state.slice(1)}</Text></Group>
            <Timeline bulletSize={16} lineWidth={2} active={detail.actions.length} mt="xs">
              <Timeline.Item title="Submitted"><Text size="xs" c="dimmed">{fmtDateShort(detail.instance.created_at)} at {fmtTime(detail.instance.created_at)}</Text></Timeline.Item>
              {detail.actions.map((a, i) => (
                <Timeline.Item key={i} title={`${a.decision.charAt(0).toUpperCase() + a.decision.slice(1)} by ${a.actor_name}`}>
                  <Text size="xs" c="dimmed">{fmtDateShort(a.acted_at)} at {fmtTime(a.acted_at)}</Text>
                  {a.reason ? <Text size="xs">“{a.reason}”</Text> : null}
                  <Text size="xs" c="dimmed" ff="monospace">Signature {a.signature.slice(0, 16)}…</Text>
                </Timeline.Item>
              ))}
            </Timeline>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
