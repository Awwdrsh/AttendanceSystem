import { Badge, Button, Group, Modal, Paper, Stack, Text, Textarea, Timeline } from '@mantine/core';
import { useState } from 'react';
import { workflow as wfApi } from '../../api';
import type { InboxItem, WorkflowDetail } from '../../api/types';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { dualDate, fmtTime, minutesToHm, relativeTime } from '../../lib/format';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const TYPE_LABEL: Record<string, [string, string]> = { leave_request: ['Leave', 'बिदा'], regularisation_request: ['Attendance correction', 'हाजिरी सुधार'], ot_request: ['Overtime', 'ओभरटाइम'], payroll_run: ['Payroll run', 'तलब रन'] };

function Summary({ item }: { item: InboxItem }) {
  const { lang, numerals } = useI18n();
  const s = item.summary as Record<string, unknown>;
  switch (item.objectType) {
    case 'leave_request': return <Text size="sm">{String(s.leaveTypeName ?? s.leaveType)} · {dualDate(String(s.from), lang, numerals).bs}{s.to !== s.from ? ` → ${dualDate(String(s.to), lang, numerals).bs}` : ''}{s.halfDay ? ' (half)' : ''} · {String(s.days)} days · balance {Number(s.balanceNow ?? 0).toFixed(1)} → {Number(s.balanceAfter ?? 0).toFixed(1)}{Array.isArray(s.teamOverlap) && s.teamOverlap.length ? <Text span c="orange"> · also off: {(s.teamOverlap as string[]).join(', ')}</Text> : null}{s.reason ? <Text span c="dimmed"> · {String(s.reason)}</Text> : null}</Text>;
    case 'regularisation_request': return <Text size="sm">{String(s.kind).replace('_', ' ')} on {dualDate(String(s.local_date), lang, numerals).bs}{s.punch_time ? ` at ${fmtTime(String(s.punch_time), numerals)} ${String(s.punch_direction ?? '').toUpperCase()}` : ''} · <Text span c="dimmed">{String(s.reason ?? '')}</Text></Text>;
    case 'ot_request': return <Text size="sm">{minutesToHm(Number(s.requested_min), numerals)} on {dualDate(String(s.local_date), lang, numerals).bs} · <Text span c="dimmed">{String(s.reason ?? '')}</Text></Text>;
    case 'payroll_run': return <Text size="sm">{String(s.run_type)} {String(s.bs_year)}-{String(s.bs_month)} · {String(s.payslips)} payslips · net {String(s.total_net)}</Text>;
    default: return <Text size="sm" c="dimmed">{JSON.stringify(s)}</Text>;
  }
}

export default function ApprovalsPage() {
  const { t, lang } = useI18n();
  const q = useQuery(() => wfApi.inbox(), []);
  const [reject, setReject] = useState<InboxItem | null>(null); const [reason, setReason] = useState('');
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);
  const items = q.data?.items ?? [];

  const approve = async (it: InboxItem) => { setBusy(it.instanceId); try { await wfApi.approve(it.instanceId); toast.ok('Approved.'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const doReject = async () => { if (!reject) return; setBusy(reject.instanceId); try { await wfApi.reject(reject.instanceId, reason); toast.ok('Rejected.'); setReject(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const bulk = async () => { setBusy('bulk'); try { const r = await wfApi.bulkApprove(items.map((i) => i.instanceId)); const ok = r.results.filter((x) => x.state).length; toast.ok(`${ok} approved${r.results.length - ok ? `, ${r.results.length - ok} failed` : ''}.`); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };

  return (
    <Stack>
      <PageHeader title={t('approvals')} subtitle={`${items.length} ${lang === 'ne' ? 'विचाराधीन' : 'pending'}`} actions={items.length > 1 ? <Button variant="light" loading={busy === 'bulk'} onClick={() => setConfirmBulk(true)}>{lang === 'ne' ? 'सबै स्वीकृत' : 'Approve all'}</Button> : null} />
      {!q.loading && items.length === 0 ? <EmptyState title={lang === 'ne' ? 'सबै स्वीकृति सकियो।' : 'Inbox is empty.'} hint={lang === 'ne' ? 'नयाँ अनुरोध आउँदा यहाँ देखिन्छ।' : 'New leave, correction and overtime requests will appear here.'} icon="✓" /> : null}
      {items.map((it) => (
        <Paper key={it.instanceId} withBorder p="md" radius="md">
          <Group justify="space-between" align="flex-start" wrap="wrap">
            <Stack gap={4} style={{ flex: 1, minWidth: 240 }}>
              <Group gap="xs"><Badge variant="light">{(TYPE_LABEL[it.objectType] ?? [it.objectType, it.objectType])[lang === 'ne' ? 1 : 0]}</Badge><Text fw={600}>{it.employee ? `${it.employee.nameEn}${it.employee.nameNe ? ` / ${it.employee.nameNe}` : ''} (${it.employee.empCode})` : '—'}</Text><Text size="xs" c="dimmed">{relativeTime(it.createdAt)} · step {it.step + 1}/{it.totalSteps}</Text></Group>
              <Summary item={it} />
            </Stack>
            <Group gap="xs">
              <Button variant="subtle" size="sm" onClick={async () => { try { setDetail(await wfApi.detail(it.instanceId)); } catch (e) { toast.err(e); } }}>Details</Button>
              <Button color="red" variant="light" h={48} loading={busy === it.instanceId} onClick={() => setReject(it)}>{t('reject')}</Button>
              <Button color="teal" h={48} loading={busy === it.instanceId} onClick={() => approve(it)}>{t('approve')}</Button>
            </Group>
          </Group>
        </Paper>
      ))}
      <Modal opened={!!reject} onClose={() => setReject(null)} title={t('reject')} centered>
        <Stack><Textarea label={t('reason')} description={t('reasonRequired')} value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} required />
          <Group justify="flex-end"><Button variant="default" onClick={() => setReject(null)}>{t('cancel')}</Button><Button color="red" disabled={reason.trim().length < 3} onClick={doReject}>{t('reject')}</Button></Group></Stack>
      </Modal>
      <ConfirmDialog opened={confirmBulk} onClose={() => setConfirmBulk(false)} color="teal" title={lang === 'ne' ? 'सबै स्वीकृत गर्ने?' : 'Approve everything?'} confirmLabel={lang === 'ne' ? 'सबै स्वीकृत' : 'Approve all'}
        message={lang === 'ne' ? `${items.length} अनुरोध स्वीकृत हुनेछन्। अस्वीकार गर्नुपर्ने छ भने पहिले त्यो गर्नुहोस्।` : `${items.length} request(s) will be approved in one go. Reject any that need it first.`} onConfirm={bulk} />
      <Modal opened={!!detail} onClose={() => setDetail(null)} title="Approval timeline" centered>
        {detail ? <Stack>
          <Text size="sm">Steps: {detail.instance.steps.map((s) => s.role).join(' → ')} · state {detail.instance.state}</Text>
          <Timeline bulletSize={18} lineWidth={2} active={detail.actions.length}>
            <Timeline.Item title="Submitted"><Text size="xs" c="dimmed">{new Date(detail.instance.created_at).toLocaleString()}</Text></Timeline.Item>
            {detail.actions.map((a, i) => <Timeline.Item key={i} title={`${a.decision} by ${a.actor_name}`}><Text size="xs" c="dimmed">{new Date(a.acted_at).toLocaleString()}{a.reason ? ` · ${a.reason}` : ''}</Text><Text size="xs" c="dimmed" ff="monospace">sig {a.signature.slice(0, 16)}…</Text></Timeline.Item>)}
          </Timeline>
        </Stack> : null}
      </Modal>
    </Stack>
  );
}
