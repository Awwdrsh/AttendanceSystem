import { Alert, Button, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Switch, Tabs, Text, TextInput, Textarea } from '@mantine/core';
import { useEffect, useState } from 'react';
import { settings as settingsApi } from '../../api';
import type { Branch, OrgUnit } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { addDays, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

type Obj = Record<string, unknown>;

/** Plain-language rule builder (plan §13.8): sentence templates with inline numbers, statutory values locked, live preview. */
function AttendanceRules({ value, onSaved }: { value: Obj; onSaved: () => void }) {
  const { t } = useI18n();
  const [p, setP] = useState<Obj>(value);
  const [preview, setPreview] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => setP(value), [value]);
  const n = (k: string) => ({ value: (p[k] as number) ?? 0, onChange: (v: number | string) => setP({ ...p, [k]: Number(v) }), w: 90, size: 'xs' as const, style: { display: 'inline-block', verticalAlign: 'middle', margin: '0 4px' } });
  const penalty = (p.latePenalty as Obj) ?? { enabled: false, lateCount: 3, deductionDays: 0.5 };
  return (
    <Stack>
      <Paper withBorder p="md" radius="md"><Text size="sm" lh={2.4}>
        <b>Overtime</b> counts after at least <NumberInput {...n('otMinThresholdMin')} min={0} max={240} /> min beyond the shift, rounded down to <NumberInput {...n('otRoundingMin')} min={0} max={60} /> min, needs <Switch size="xs" display="inline-block" style={{ verticalAlign: 'middle' }} checked={!!p.requirePreapproval} onChange={(e) => setP({ ...p, requirePreapproval: e.currentTarget.checked })} label="manager pre-approval" />, max <b>4 h/day and 24 h/week</b> <Text span size="xs" c="dimmed">🔒 Labour Act Sec 30(1)</Text>, paid at <b>1.5× basic</b> <Text span size="xs" c="dimmed">🔒 Sec 31(1)</Text>.
      </Text></Paper>
      <Paper withBorder p="md" radius="md"><Text size="sm" lh={2.4}>
        Work on a <b>holiday or weekly off</b> gives <Select size="xs" display="inline-block" style={{ verticalAlign: 'middle' }} w={200} value={String(p.holidayWork ?? 'substitute_leave')} onChange={(v) => v && setP({ ...p, holidayWork: v })} allowDeselect={false} data={[{ value: 'substitute_leave', label: 'substitute leave (Sec 42)' }, { value: 'pay', label: '1.5× pay' }, { value: 'employee_choice', label: 'employee choice' }]} />.
        Night work 22:00–04:00 premium: <Switch size="xs" display="inline-block" style={{ verticalAlign: 'middle' }} checked={!!p.nightWindowEnabled} onChange={(e) => setP({ ...p, nightWindowEnabled: e.currentTarget.checked })} label="enabled" /> at rate <NumberInput {...n('nightPremiumRate')} min={0} max={1} step={0.05} decimalScale={2} /> <Text span size="xs" c="dimmed">(company policy, not statutory)</Text>.
      </Text></Paper>
      <Paper withBorder p="md" radius="md"><Text size="sm" lh={2.4}>
        <b>Auto clock-out</b> at shift end + <NumberInput {...n('autoClockOutAfterMin')} min={0} max={720} /> min if no OUT punch (0 = off) and raise a missing-punch exception.
        Employees may request <b>corrections</b> within <NumberInput {...n('regularisationBackdateDays')} min={0} max={90} /> days, at most <NumberInput {...n('regularisationMonthlyCap')} min={0} max={31} /> per month.
      </Text></Paper>
      <Paper withBorder p="md" radius="md"><Text size="sm" lh={2.4}>
        <Switch size="xs" display="inline-block" style={{ verticalAlign: 'middle' }} checked={!!penalty.enabled} onChange={(e) => setP({ ...p, latePenalty: { ...penalty, enabled: e.currentTarget.checked } })} label="Late penalty" />: late <NumberInput size="xs" w={70} display="inline-block" style={{ verticalAlign: 'middle' }} value={Number(penalty.lateCount)} onChange={(v) => setP({ ...p, latePenalty: { ...penalty, lateCount: Number(v) } })} min={1} /> times in a month = <NumberInput size="xs" w={80} display="inline-block" style={{ verticalAlign: 'middle' }} value={Number(penalty.deductionDays)} onChange={(v) => setP({ ...p, latePenalty: { ...penalty, deductionDays: Number(v) } })} min={0} max={1} step={0.5} decimalScale={1} /> day deduction <Text span size="xs" c="dimmed">(Sec 38(e); applied from the next period)</Text>.
      </Text></Paper>
      {preview ? <Alert variant="light" color="blue" title="Applying to the last 30 days">{preview}</Alert> : null}
      <Group justify="flex-end">
        <Button variant="light" loading={busy} onClick={async () => { setBusy(true); try { const r = await settingsApi.previewAttendance(p, addDays(todayNpt(), -30), todayNpt()); setPreview(`${r.summary} ${r.affectedEmployees} of ${r.employees} employees would change.`); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('preview')}</Button>
        <Button loading={busy} onClick={async () => { setBusy(true); try { await settingsApi.put('attendance.policy', p); toast.ok('Attendance rules saved; they apply to future recomputes.'); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('save')}</Button>
      </Group>
    </Stack>
  );
}

function GenericSettings({ settingKey, value, fields, onSaved }: { settingKey: string; value: Obj; fields: Array<{ key: string; label: string; type: 'number' | 'boolean' | 'select' | 'text'; options?: string[]; hint?: string }>; onSaved: () => void }) {
  const { t } = useI18n();
  const [p, setP] = useState<Obj>(value); const [busy, setBusy] = useState(false);
  useEffect(() => setP(value), [value]);
  return (
    <Stack>
      <SimpleGrid cols={{ base: 1, sm: 2 }}>
        {fields.map((f) => f.type === 'boolean' ? <Switch key={f.key} label={f.label} description={f.hint} checked={!!p[f.key]} onChange={(e) => setP({ ...p, [f.key]: e.currentTarget.checked })} />
          : f.type === 'number' ? <NumberInput key={f.key} label={f.label} description={f.hint} value={(p[f.key] as number) ?? 0} onChange={(v) => setP({ ...p, [f.key]: Number(v) })} />
          : f.type === 'select' ? <Select key={f.key} label={f.label} description={f.hint} value={String(p[f.key] ?? '')} onChange={(v) => v && setP({ ...p, [f.key]: v })} data={f.options ?? []} allowDeselect={false} />
          : <TextInput key={f.key} label={f.label} description={f.hint} value={String(p[f.key] ?? '')} onChange={(e) => setP({ ...p, [f.key]: e.currentTarget.value })} />)}
      </SimpleGrid>
      <Group justify="flex-end"><Button loading={busy} onClick={async () => { setBusy(true); try { const body: Obj = {}; for (const [k, v] of Object.entries(p)) if (v !== '' && v !== undefined) body[k] = v; await settingsApi.put(settingKey, body); toast.ok('Saved.'); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('save')}</Button></Group>
    </Stack>
  );
}

export default function SettingsPage() {
  const { t } = useI18n();
  const q = useQuery(() => settingsApi.all(), []);
  const branches = useQuery(() => settingsApi.branches(), []);
  const units = useQuery(() => settingsApi.orgUnits(), []);
  const [bCode, setBCode] = useState(''); const [bName, setBName] = useState(''); const [uCode, setUCode] = useState(''); const [uName, setUName] = useState(''); const [uKind, setUKind] = useState('department');
  const val = (k: string): Obj => (q.data?.settings.find((s) => s.key === k)?.value as Obj) ?? {};
  return (
    <Stack>
      <PageHeader title={t('rules')} subtitle="Statutory values are shown locked with their citation; closed payroll periods are never recalculated." />
      <Tabs defaultValue="attendance" keepMounted={false}>
        <Tabs.List><Tabs.Tab value="attendance">Attendance</Tabs.Tab><Tabs.Tab value="leave">Leave</Tabs.Tab><Tabs.Tab value="payroll">Payroll</Tabs.Tab><Tabs.Tab value="notify">Notifications</Tabs.Tab><Tabs.Tab value="company">Company</Tabs.Tab><Tabs.Tab value="org">Branches & units</Tabs.Tab></Tabs.List>
        <Tabs.Panel value="attendance" pt="md">{q.data ? <AttendanceRules value={val('attendance.policy')} onSaved={() => void q.reload()} /> : null}</Tabs.Panel>
        <Tabs.Panel value="leave" pt="md">{q.data ? <GenericSettings settingKey="leave.policy" value={val('leave.policy')} onSaved={() => void q.reload()} fields={[
          { key: 'leaveYear', label: 'Leave year', type: 'select', options: ['fiscal', 'anniversary'] }, { key: 'sandwichRule', label: 'Sandwich rule by default', type: 'boolean', hint: 'Count weekly offs and holidays between leave days' },
          { key: 'probationRestriction', label: 'No paid company leave during probation', type: 'boolean' }, { key: 'maxContinuousDays', label: 'Max continuous days', type: 'number' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="payroll" pt="md">{q.data ? <GenericSettings settingKey="payroll.policy" value={val('payroll.policy')} onSaved={() => void q.reload()} fields={[
          { key: 'prorationBasis', label: 'Proration basis', type: 'select', options: ['bs_calendar_days', 'fixed_30', '26_day'], hint: 'Default: calendar days of the BS month (29–32)' }, { key: 'hourlyDivisor', label: 'Hourly divisor for OT', type: 'number', hint: 'Default 208 (26 × 8); alternative 240' },
          { key: 'payDay', label: 'Pay day (BS)', type: 'number' }, { key: 'rounding', label: 'Rounding', type: 'select', options: ['half_up_rupee', 'paisa'] }, { key: 'twoApproverThreshold', label: 'Second approver above (NPR)', type: 'number' }, { key: 'encashmentDivisor', label: 'Encashment divisor', type: 'select', options: ['bs_days', 'fixed_30'] }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="notify" pt="md">{q.data ? <GenericSettings settingKey="notifications.policy" value={val('notifications.policy')} onSaved={() => void q.reload()} fields={[
          { key: 'quietHoursStart', label: 'Quiet hours start', type: 'text' }, { key: 'quietHoursEnd', label: 'Quiet hours end', type: 'text' }, { key: 'slaHours', label: 'Approval reminder after (h)', type: 'number' }, { key: 'escalationHours', label: 'Escalate to HR after (h)', type: 'number' }, { key: 'digestTime', label: 'Manager digest time', type: 'text' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="company" pt="md">{q.data ? <GenericSettings settingKey="company.profile" value={val('company.profile')} onSaved={() => void q.reload()} fields={[
          { key: 'nameEn', label: 'Company name', type: 'text' }, { key: 'nameNe', label: 'कम्पनीको नाम', type: 'text' }, { key: 'address', label: 'Address', type: 'text' }, { key: 'pan', label: 'Company PAN', type: 'text' }, { key: 'ssfEmployerId', label: 'SSF employer ID', type: 'text' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="org" pt="md">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Stack>
              <Text fw={600}>Branches</Text>
              <DataTable<Branch> rows={branches.data?.branches ?? []} rowKey={(b) => b.id} loading={branches.loading} emptyTitle="No branches." columns={[{ key: 'code', header: t('code') }, { key: 'name_en', header: t('name') }]} maxHeight={300} />
              <Group><TextInput placeholder="Code" value={bCode} onChange={(e) => setBCode(e.currentTarget.value.toUpperCase())} w={100} /><TextInput placeholder="Name" value={bName} onChange={(e) => setBName(e.currentTarget.value)} /><Button disabled={!bCode || !bName} onClick={async () => { try { await settingsApi.createBranch({ code: bCode, nameEn: bName }); setBCode(''); setBName(''); await branches.reload(); } catch (e) { toast.err(e); } }}>{t('create')}</Button></Group>
            </Stack>
            <Stack>
              <Text fw={600}>Departments & teams</Text>
              <DataTable<OrgUnit> rows={units.data?.orgUnits ?? []} rowKey={(u) => u.id} loading={units.loading} emptyTitle="No units." columns={[{ key: 'kind', header: 'Kind' }, { key: 'code', header: t('code') }, { key: 'name_en', header: t('name') }]} maxHeight={300} />
              <Group><Select value={uKind} onChange={(v) => v && setUKind(v)} data={['department', 'team']} allowDeselect={false} w={130} /><TextInput placeholder="Code" value={uCode} onChange={(e) => setUCode(e.currentTarget.value.toUpperCase())} w={100} /><TextInput placeholder="Name" value={uName} onChange={(e) => setUName(e.currentTarget.value)} /><Button disabled={!uCode || !uName} onClick={async () => { try { await settingsApi.createOrgUnit({ kind: uKind, code: uCode, nameEn: uName }); setUCode(''); setUName(''); await units.reload(); } catch (e) { toast.err(e); } }}>{t('create')}</Button></Group>
            </Stack>
          </SimpleGrid>
        </Tabs.Panel>
      </Tabs>
      <Textarea display="none" />
    </Stack>
  );
}
