import { Button, Group, Paper, SimpleGrid, Stack, Tabs, Text } from '@mantine/core';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { biometrics as bioApi, employees as empApi, leave as leaveApi, payroll as payApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeeForm } from '../../components/EmployeeForm';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { fmtMoney, fmtNum, dualDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { LeaveApplyForm } from '../employee/LeavePage';

export default function EmployeeDetailPage() {
  const { id = '' } = useParams();
  const { t, lang, numerals } = useI18n();
  const { hasRole } = useAuth();
  const emp = useQuery(() => empApi.get(id), [id]);
  const balances = useQuery(() => leaveApi.balances(id), [id]);
  const assignments = useQuery(() => empApi.assignments(id), [id]);
  const audit = useQuery(() => empApi.audit(id), [id]);
  const templates = useQuery(() => bioApi.templates(id), [id]);
  const consents = useQuery(() => bioApi.consents(id), [id]);
  const payslips = useQuery(() => payApi.payslips({ employeeId: id }), [id], { enabled: hasRole('hr', 'finance') });
  const [sensitive, setSensitive] = useState<{ pan: string | null; bankAccount: string | null } | null>(null);
  const [busy, setBusy] = useState(false); const [shred, setShred] = useState(false);
  const e = emp.data?.employee;
  if (emp.error) return <EmptyState title="Employee not found" hint={emp.error} />;
  if (!e) return null;
  return (
    <Stack>
      <PageHeader title={`${e.name_en}${e.name_ne ? ` / ${e.name_ne}` : ''}`} subtitle={`${e.emp_code} · ${e.designation ?? ''} · joined ${e.join_date ? dualDate(e.join_date, lang, numerals).bsLong : '—'}`}
        actions={<Group gap="xs"><StatusBadge status={e.status} /><Button component={Link} to={`/me/payslips?employeeId=${id}`} variant="light" size="xs">{t('payslips')}</Button></Group>} />
      <Tabs defaultValue="profile" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="profile">{t('profile')}</Tabs.Tab><Tabs.Tab value="leave">{t('leave')}</Tabs.Tab><Tabs.Tab value="assignments">History</Tabs.Tab>
          <Tabs.Tab value="pay">Pay</Tabs.Tab><Tabs.Tab value="biometrics">Biometrics</Tabs.Tab><Tabs.Tab value="audit">Audit</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="profile" pt="md">
          <Paper withBorder p="md" radius="md" mb="md">
            <Group justify="space-between"><Text fw={600}>Sensitive fields</Text>
              {sensitive ? <Text size="sm">PAN {sensitive.pan ?? '—'} · Bank account {sensitive.bankAccount ?? '—'}</Text> : <Button size="xs" variant="light" onClick={async () => { try { setSensitive(await empApi.sensitive(id)); } catch (err) { toast.err(err); } }}>Reveal (logged)</Button>}
            </Group>
          </Paper>
          <EmployeeForm initial={e} busy={busy} onSubmit={async (draft) => { setBusy(true); try { await empApi.update(id, draft); toast.ok('Saved.'); await emp.reload(); } catch (err) { toast.err(err); } finally { setBusy(false); } }} />
        </Tabs.Panel>
        <Tabs.Panel value="leave" pt="md">
          <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="sm" mb="md">
            {(balances.data?.balances ?? []).map((b) => <Paper key={b.leaveTypeId} withBorder p="sm" radius="md"><Text size="xs" c="dimmed">{b.nameEn}</Text><Text fw={700}>{fmtNum(b.available.toFixed(1), numerals)} {t('days')}</Text><Text size="xs" c="dimmed">{b.explanation}</Text></Paper>)}
          </SimpleGrid>
          <Paper withBorder p="md" radius="md"><Text fw={600} mb="sm">Apply on behalf</Text><LeaveApplyForm employeeId={id} onDone={() => { void balances.reload(); }} /></Paper>
        </Tabs.Panel>
        <Tabs.Panel value="assignments" pt="md">
          <DataTable rows={(assignments.data?.assignments ?? []) as Array<Record<string, unknown>>} rowKey={(r) => String(r.valid)} loading={assignments.loading} emptyTitle="No assignment history."
            columns={[{ key: 'valid', header: 'Period', render: (r) => String(r.valid) }, { key: 'branch_id', header: 'Branch', render: (r) => String(r.branch_id ?? '—') }, { key: 'employment_type', header: 'Type', render: (r) => String(r.employment_type) }, { key: 'manager_id', header: 'Manager', render: (r) => String(r.manager_id ?? '—') }]} />
        </Tabs.Panel>
        <Tabs.Panel value="pay" pt="md">
          <Group mb="sm"><Button component={Link} to={`/payroll/assignments?employeeId=${id}`} variant="light" size="xs">{t('assignments')}</Button><Button component={Link} to={`/payroll/loans?employeeId=${id}`} variant="light" size="xs">{t('loans')}</Button></Group>
          <DataTable rows={payslips.data?.payslips ?? []} rowKey={(r) => r.id} loading={payslips.loading} emptyTitle="No payslips yet."
            columns={[{ key: 'p', header: t('month'), render: (r) => `${r.bs_year}-${String(r.bs_month).padStart(2, '0')} ${r.run_type ?? ''}` }, { key: 'net', header: t('netPay'), render: (r) => fmtMoney(r.net, { numerals }), align: 'right' }, { key: 'pub', header: 'Published', render: (r) => r.published_at ? 'yes' : 'no' }, { key: 'open', header: '', render: (r) => <Button component={Link} to={`/me/payslips/${r.id}`} size="xs" variant="subtle">Open</Button> }]} />
        </Tabs.Panel>
        <Tabs.Panel value="biometrics" pt="md">
          <Stack>
            <Group><Button size="xs" variant="light" onClick={async () => { try { await bioApi.consent({ employeeId: id, modality: 'face', noticeVersion: '2026-10' }); toast.ok('Consent recorded.'); await consents.reload(); } catch (err) { toast.err(err); } }}>Record face consent</Button>
              <Button size="xs" variant="light" onClick={async () => { try { await bioApi.consent({ employeeId: id, modality: 'fingerprint', noticeVersion: '2026-10' }); toast.ok('Consent recorded.'); await consents.reload(); } catch (err) { toast.err(err); } }}>Record fingerprint consent</Button>
              <Button size="xs" color="red" variant="light" onClick={() => setShred(true)}>Shred all templates</Button></Group>
            <Text size="sm" fw={600}>Consents</Text>
            {(consents.data?.consents ?? []).map((c) => <Text key={c.id} size="sm">{c.modality} v{c.notice_version} · {new Date(c.granted_at).toLocaleDateString()}{c.revoked_at ? ' · withdrawn' : ''}</Text>)}
            <Text size="sm" fw={600}>Templates</Text>
            <DataTable rows={templates.data?.templates ?? []} rowKey={(r) => r.id} loading={templates.loading} emptyTitle="No templates stored."
              columns={[{ key: 'modality', header: 'Modality' }, { key: 'vendor_format', header: 'Format' }, { key: 'device_sn', header: 'Device', render: (r) => r.device_sn ?? '—' }, { key: 'created_at', header: 'Enrolled', render: (r) => new Date(r.created_at).toLocaleDateString() }, { key: 'shredded_at', header: 'Shredded', render: (r) => r.shredded_at ? new Date(r.shredded_at).toLocaleDateString() : '—' }]} />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="audit" pt="md">
          <DataTable rows={audit.data?.events ?? []} rowKey={(r) => String(r.id)} loading={audit.loading} emptyTitle="No audit events."
            columns={[{ key: 'occurred_at', header: 'When', render: (r) => new Date(r.occurred_at).toLocaleString() }, { key: 'action', header: 'Action' }, { key: 'table_name', header: 'Record' }, { key: 'actor_id', header: 'Actor', render: (r) => r.actor_id ?? 'system' }]} />
        </Tabs.Panel>
      </Tabs>
      <ConfirmDialog opened={shred} onClose={() => setShred(false)} title="Shred all biometric templates" confirmLabel="Shred" message={`All stored templates for ${e.name_en} will be cryptographically destroyed and consents withdrawn. The employee will need re-enrolment to use face or fingerprint again.`}
        onConfirm={async () => { try { const r = await bioApi.shredAll(id); toast.ok(`${r.shredded} template(s) shredded.`); await Promise.all([templates.reload(), consents.reload()]); } catch (err) { toast.err(err); } }} />
    </Stack>
  );
}
