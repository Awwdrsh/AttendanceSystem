import { Alert, Box, Button, Flex, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Switch, Tabs, Text, TextInput } from '@mantine/core';
import { IconEye, IconLock, IconPlus } from '@tabler/icons-react';
import { useEffect, useState, type ReactNode } from 'react';
import { settings as settingsApi } from '../../api';
import type { Branch, OrgUnit } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { SecurityPolicySettings } from '../../components/SecurityPolicySettings';
import { addDays, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { TimeTrackingSettings } from '../time/TimeSettings';

type Obj = Record<string, unknown>;
const ICON = { size: 16, stroke: 1.6 } as const;

/** One panel of settings with a heading and hairline-separated rows. */
function SettingsSection({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <Paper withBorder>
      <Box px="lg" py="md" style={{ borderBottom: '1px solid var(--hj-line)' }}>
        <Text fw={600}>{title}</Text>
        {description ? <Text size="sm" c="dimmed">{description}</Text> : null}
      </Box>
      <div>{children}</div>
    </Paper>
  );
}

/** Label and explanation on the left, the control on the right; stacks on small screens. */
function SettingRow({ label, description, children }: { label: ReactNode; description?: ReactNode; children: ReactNode }) {
  return (
    <Flex direction={{ base: 'column', sm: 'row' }} gap={{ base: 8, sm: 'xl' }} justify="space-between" align={{ base: 'stretch', sm: 'center' }} px="lg" py="md" style={{ borderTop: '1px solid var(--hj-line-soft)' }}>
      <Box style={{ flex: 1, minWidth: 0 }}>
        <Text size="sm" fw={500}>{label}</Text>
        {description ? <Text size="xs" c="dimmed" mt={2}>{description}</Text> : null}
      </Box>
      <Box style={{ flex: 'none' }}>{children}</Box>
    </Flex>
  );
}

/** A value fixed by law, shown read-only with its citation. */
function Locked({ children, cite }: { children: ReactNode; cite: string }) {
  return (
    <Group gap={6} wrap="nowrap">
      <IconLock size={14} stroke={1.6} color="var(--hj-muted)" />
      <div style={{ textAlign: 'right' }}>
        <Text size="sm" fw={500}>{children}</Text>
        <Text size="xs" c="dimmed">{cite}</Text>
      </div>
    </Group>
  );
}

const CONTROL_W = 200;

function AttendanceRules({ value, onSaved }: { value: Obj; onSaved: () => void }) {
  const [p, setP] = useState<Obj>(value);
  const [preview, setPreview] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => setP(value), [value]);
  const n = (k: string, extra: { min?: number; max?: number; step?: number; decimalScale?: number; suffix?: string } = {}) => (
    <NumberInput w={CONTROL_W} value={(p[k] as number) ?? 0} onChange={(v) => setP({ ...p, [k]: Number(v) })} {...extra} aria-label={k} />
  );
  const penalty = (p.latePenalty as Obj) ?? { enabled: false, lateCount: 3, deductionDays: 0.5 };
  return (
    <Stack gap="lg">
      <SettingsSection title="Overtime" description="When extra time beyond the shift counts as overtime.">
        <SettingRow label="Minimum overtime" description="Shorter stays after the shift are ignored.">{n('otMinThresholdMin', { min: 0, max: 240, suffix: ' min' })}</SettingRow>
        <SettingRow label="Round overtime down to" description="Overtime is rounded down to this step.">{n('otRoundingMin', { min: 0, max: 60, suffix: ' min' })}</SettingRow>
        <SettingRow label="Require manager pre-approval" description="Overtime without approval is not paid.">
          <Switch checked={!!p.requirePreapproval} onChange={(e) => setP({ ...p, requirePreapproval: e.currentTarget.checked })} aria-label="Require manager pre-approval" />
        </SettingRow>
        <SettingRow label="Maximum overtime"><Locked cite="Labour Act Sec 30(1)">4 h a day, 24 h a week</Locked></SettingRow>
        <SettingRow label="Overtime pay"><Locked cite="Labour Act Sec 31(1)">1.5 times basic</Locked></SettingRow>
      </SettingsSection>

      <SettingsSection title="Holidays and night work">
        <SettingRow label="Work on a holiday or weekly off" description="What employees get when they work on a day off.">
          <Select w={CONTROL_W} value={String(p.holidayWork ?? 'substitute_leave')} onChange={(v) => v && setP({ ...p, holidayWork: v })} allowDeselect={false} aria-label="Work on a holiday or weekly off"
            data={[{ value: 'substitute_leave', label: 'Substitute leave (Sec 42)' }, { value: 'pay', label: '1.5 times pay' }, { value: 'employee_choice', label: "Employee's choice" }]} />
        </SettingRow>
        <SettingRow label="Night work premium" description="Pay extra for hours worked between 22:00 and 04:00. Company policy, not statutory.">
          <Switch checked={!!p.nightWindowEnabled} onChange={(e) => setP({ ...p, nightWindowEnabled: e.currentTarget.checked })} aria-label="Night work premium" />
        </SettingRow>
        <SettingRow label="Night premium rate" description="Added to the hourly rate, for example 0.25 for 25% extra.">{n('nightPremiumRate', { min: 0, max: 1, step: 0.05, decimalScale: 2 })}</SettingRow>
      </SettingsSection>

      <SettingsSection title="Missing punches and corrections">
        <SettingRow label="Auto clock-out" description="If nobody clocks out, close the day this long after shift end and flag it as a missing punch. 0 turns it off.">{n('autoClockOutAfterMin', { min: 0, max: 720, suffix: ' min' })}</SettingRow>
        <SettingRow label="Correction window" description="How far back employees can request an attendance correction.">{n('regularisationBackdateDays', { min: 0, max: 90, suffix: ' days' })}</SettingRow>
        <SettingRow label="Corrections per month" description="Maximum attendance corrections an employee can request each month.">{n('regularisationMonthlyCap', { min: 0, max: 31 })}</SettingRow>
      </SettingsSection>

      <SettingsSection title="Work from home">
        <SettingRow label="Photo at clock-in" description="Ask for a quick photo when someone clocks in from home. Managers and HR see it under Attendance, Work from home.">
          <Switch checked={p.wfhPhotoRequired !== false} onChange={(e) => setP({ ...p, wfhPhotoRequired: e.currentTarget.checked })} aria-label="Photo at clock-in" />
        </SettingRow>
        <SettingRow label="Keep photos for" description="Photos are deleted automatically after this many days.">{n('wfhPhotoRetentionDays', { min: 7, max: 365, suffix: ' days' })}</SettingRow>
      </SettingsSection>

      <SettingsSection title="Late penalty" description="Deduct pay for repeated late arrivals (Sec 38(e)). Applies from the next payroll period.">
        <SettingRow label="Apply late penalty">
          <Switch checked={!!penalty.enabled} onChange={(e) => setP({ ...p, latePenalty: { ...penalty, enabled: e.currentTarget.checked } })} aria-label="Apply late penalty" />
        </SettingRow>
        <SettingRow label="Late arrivals per month" description="Number of late days that triggers a deduction.">
          <NumberInput w={CONTROL_W} value={Number(penalty.lateCount)} onChange={(v) => setP({ ...p, latePenalty: { ...penalty, lateCount: Number(v) } })} min={1} disabled={!penalty.enabled} aria-label="Late arrivals per month" />
        </SettingRow>
        <SettingRow label="Deduction" description="Days of pay deducted each time.">
          <NumberInput w={CONTROL_W} value={Number(penalty.deductionDays)} onChange={(v) => setP({ ...p, latePenalty: { ...penalty, deductionDays: Number(v) } })} min={0} max={1} step={0.5} decimalScale={1} suffix=" day" disabled={!penalty.enabled} aria-label="Deduction" />
        </SettingRow>
      </SettingsSection>

      {preview ? <Alert variant="light" color="blue" title="Effect on the last 30 days">{preview}</Alert> : null}
      <Group justify="flex-end">
        <Button variant="default" leftSection={<IconEye {...ICON} />} loading={busy} onClick={async () => { setBusy(true); try { const r = await settingsApi.previewAttendance(p, addDays(todayNpt(), -30), todayNpt()); setPreview(`${r.summary} ${r.affectedEmployees} of ${r.employees} employees would change.`); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Preview impact</Button>
        <Button loading={busy} onClick={async () => { setBusy(true); try { await settingsApi.put('attendance.policy', p); toast.ok('Attendance rules saved. They apply to future recalculations.'); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Save rules</Button>
      </Group>
    </Stack>
  );
}

type FieldDef = { key: string; label: string; type: 'number' | 'boolean' | 'select' | 'text'; options?: Array<string | { value: string; label: string }>; hint?: string; suffix?: string };

function GenericSettings({ settingKey, title, description, value, fields, onSaved }: { settingKey: string; title: string; description?: string; value: Obj; fields: FieldDef[]; onSaved: () => void }) {
  const [p, setP] = useState<Obj>(value); const [busy, setBusy] = useState(false);
  useEffect(() => setP(value), [value]);
  const control = (f: FieldDef) => {
    if (f.type === 'boolean') return <Switch checked={!!p[f.key]} onChange={(e) => setP({ ...p, [f.key]: e.currentTarget.checked })} aria-label={f.label} />;
    if (f.type === 'number') return <NumberInput w={CONTROL_W} value={(p[f.key] as number) ?? 0} onChange={(v) => setP({ ...p, [f.key]: Number(v) })} aria-label={f.label} {...(f.suffix ? { suffix: f.suffix } : {})} />;
    if (f.type === 'select') return <Select w={CONTROL_W} value={String(p[f.key] ?? '')} onChange={(v) => v && setP({ ...p, [f.key]: v })} data={f.options ?? []} allowDeselect={false} aria-label={f.label} />;
    return <TextInput w={{ base: '100%', sm: 280 }} value={String(p[f.key] ?? '')} onChange={(e) => setP({ ...p, [f.key]: e.currentTarget.value })} aria-label={f.label} />;
  };
  return (
    <Stack gap="lg">
      <SettingsSection title={title} {...(description ? { description } : {})}>
        {fields.map((f) => <SettingRow key={f.key} label={f.label} {...(f.hint ? { description: f.hint } : {})}>{control(f)}</SettingRow>)}
      </SettingsSection>
      <Group justify="flex-end"><Button loading={busy} onClick={async () => { setBusy(true); try { const body: Obj = {}; for (const [k, v] of Object.entries(p)) if (v !== '' && v !== undefined) body[k] = v; await settingsApi.put(settingKey, body); toast.ok('Settings saved.'); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Save changes</Button></Group>
    </Stack>
  );
}

export default function SettingsPage() {
  const q = useQuery(() => settingsApi.all(), []);
  const branches = useQuery(() => settingsApi.branches(), []);
  const units = useQuery(() => settingsApi.orgUnits(), []);
  const [bCode, setBCode] = useState(''); const [bName, setBName] = useState(''); const [uCode, setUCode] = useState(''); const [uName, setUName] = useState(''); const [uKind, setUKind] = useState('department');
  const val = (k: string): Obj => (q.data?.settings.find((s) => s.key === k)?.value as Obj) ?? {};
  const reload = () => void q.reload();
  return (
    <Stack gap="lg">
      <PageHeader title="Attendance rules" subtitle="How attendance, leave and pay are calculated. Values fixed by law are locked with their citation, and closed payroll periods are never recalculated." />
      <Tabs defaultValue="attendance" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="attendance">Attendance</Tabs.Tab>
          <Tabs.Tab value="leave">Leave</Tabs.Tab>
          <Tabs.Tab value="payroll">Payroll</Tabs.Tab>
          <Tabs.Tab value="time">Time tracking</Tabs.Tab>
          <Tabs.Tab value="notify">Notifications</Tabs.Tab>
          <Tabs.Tab value="company">Company</Tabs.Tab>
          <Tabs.Tab value="org">Branches and units</Tabs.Tab>
          <Tabs.Tab value="security">Security</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="attendance" pt="lg">{q.data ? <AttendanceRules value={val('attendance.policy')} onSaved={reload} /> : null}</Tabs.Panel>
        <Tabs.Panel value="leave" pt="lg">{q.data ? <GenericSettings settingKey="leave.policy" title="Leave" description="Defaults that apply to every leave type unless the type overrides them." value={val('leave.policy')} onSaved={reload} fields={[
          { key: 'leaveYear', label: 'Leave year', type: 'select', hint: 'When balances reset and carry forward.', options: [{ value: 'fiscal', label: 'Fiscal year' }, { value: 'anniversary', label: 'Joining anniversary' }] },
          { key: 'sandwichRule', label: 'Sandwich rule by default', type: 'boolean', hint: 'Count weekly offs and holidays between leave days.' },
          { key: 'probationRestriction', label: 'No paid company leave during probation', type: 'boolean', hint: 'Statutory leave is still allowed.' },
          { key: 'maxContinuousDays', label: 'Max continuous days', type: 'number', hint: 'Longest single leave request.', suffix: ' days' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="payroll" pt="lg">{q.data ? <GenericSettings settingKey="payroll.policy" title="Payroll" description="How salaries are prorated, rounded and approved." value={val('payroll.policy')} onSaved={reload} fields={[
          { key: 'prorationBasis', label: 'Proration basis', type: 'select', hint: 'Default: calendar days of the Nepali month (29 to 32).', options: [{ value: 'bs_calendar_days', label: 'Calendar days' }, { value: 'fixed_30', label: 'Fixed 30 days' }, { value: '26_day', label: '26 working days' }] },
          { key: 'hourlyDivisor', label: 'Hourly divisor for overtime', type: 'number', hint: 'Default 208 (26 × 8); 240 is also common.' },
          { key: 'payDay', label: 'Pay day', type: 'number', hint: 'Day of the Nepali month salaries are paid.' },
          { key: 'rounding', label: 'Rounding', type: 'select', options: [{ value: 'half_up_rupee', label: 'Nearest rupee' }, { value: 'paisa', label: 'Exact to the paisa' }] },
          { key: 'twoApproverThreshold', label: 'Second approver above', type: 'number', hint: 'Payroll runs with a net total above this amount need two approvers.', suffix: ' NPR' },
          { key: 'encashmentDivisor', label: 'Leave encashment divisor', type: 'select', options: [{ value: 'bs_days', label: 'Days in the month' }, { value: 'fixed_30', label: 'Fixed 30 days' }] }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="time" pt="lg"><TimeTrackingSettings /></Tabs.Panel>
        <Tabs.Panel value="notify" pt="lg">{q.data ? <GenericSettings settingKey="notifications.policy" title="Notifications" description="When reminders and digests go out." value={val('notifications.policy')} onSaved={reload} fields={[
          { key: 'quietHoursStart', label: 'Quiet hours start', type: 'text', hint: 'HH:MM. No push notifications during quiet hours.' },
          { key: 'quietHoursEnd', label: 'Quiet hours end', type: 'text', hint: 'HH:MM' },
          { key: 'slaHours', label: 'Remind approvers after', type: 'number', hint: 'Pending requests trigger a reminder after this long.', suffix: ' h' },
          { key: 'escalationHours', label: 'Escalate to HR after', type: 'number', suffix: ' h' },
          { key: 'digestTime', label: 'Manager digest time', type: 'text', hint: 'HH:MM. Daily summary sent to managers.' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="company" pt="lg">{q.data ? <GenericSettings settingKey="company.profile" title="Company" description="Shown on payslips and statutory reports." value={val('company.profile')} onSaved={reload} fields={[
          { key: 'nameEn', label: 'Company name', type: 'text' },
          { key: 'address', label: 'Address', type: 'text' },
          { key: 'pan', label: 'Company PAN', type: 'text', hint: 'Permanent account number from the Inland Revenue Department.' },
          { key: 'ssfEmployerId', label: 'SSF employer ID', type: 'text', hint: 'Social Security Fund registration number.' }]} /> : null}</Tabs.Panel>
        <Tabs.Panel value="security" pt="lg">{q.data ? <SecurityPolicySettings value={val('security.policy')} onSaved={reload} /> : null}</Tabs.Panel>
        <Tabs.Panel value="org" pt="lg">
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            <Stack gap="sm">
              <SectionTitle hint="Offices and sites employees belong to.">Branches</SectionTitle>
              <DataTable<Branch> rows={branches.data?.branches ?? []} rowKey={(b) => b.id} loading={branches.loading} error={branches.error} emptyTitle="No branches yet" emptyHint="Add your first branch below." maxHeight={300}
                columns={[{ key: 'name_en', header: 'Name', render: (b) => <Text size="sm" fw={500}>{b.name_en}</Text> }, { key: 'code', header: 'Code', render: (b) => <Text size="sm" c="dimmed" ff="monospace">{b.code}</Text> }]} />
              <Group gap="xs" wrap="wrap">
                <TextInput placeholder="Code" aria-label="Branch code" value={bCode} onChange={(e) => setBCode(e.currentTarget.value.toUpperCase())} w={100} />
                <TextInput placeholder="Branch name" aria-label="Branch name" value={bName} onChange={(e) => setBName(e.currentTarget.value)} style={{ flex: 1, minWidth: 140 }} />
                <Button variant="default" leftSection={<IconPlus {...ICON} />} disabled={!bCode || !bName} onClick={async () => { try { await settingsApi.createBranch({ code: bCode, nameEn: bName }); setBCode(''); setBName(''); await branches.reload(); } catch (e) { toast.err(e); } }}>Add branch</Button>
              </Group>
            </Stack>
            <Stack gap="sm">
              <SectionTitle hint="Used for approvals, reports and filters.">Departments and teams</SectionTitle>
              <DataTable<OrgUnit> rows={units.data?.orgUnits ?? []} rowKey={(u) => u.id} loading={units.loading} error={units.error} emptyTitle="No departments or teams yet" emptyHint="Add one below." maxHeight={300}
                columns={[{ key: 'name_en', header: 'Name', render: (u) => <Text size="sm" fw={500}>{u.name_en}</Text> }, { key: 'code', header: 'Code', render: (u) => <Text size="sm" c="dimmed" ff="monospace">{u.code}</Text> }, { key: 'kind', header: 'Kind', render: (u) => <Text size="sm" c="dimmed">{u.kind === 'team' ? 'Team' : 'Department'}</Text> }]} />
              <Group gap="xs" wrap="wrap">
                <Select value={uKind} onChange={(v) => v && setUKind(v)} aria-label="Kind" data={[{ value: 'department', label: 'Department' }, { value: 'team', label: 'Team' }]} allowDeselect={false} w={130} />
                <TextInput placeholder="Code" aria-label="Unit code" value={uCode} onChange={(e) => setUCode(e.currentTarget.value.toUpperCase())} w={100} />
                <TextInput placeholder="Name" aria-label="Unit name" value={uName} onChange={(e) => setUName(e.currentTarget.value)} style={{ flex: 1, minWidth: 140 }} />
                <Button variant="default" leftSection={<IconPlus {...ICON} />} disabled={!uCode || !uName} onClick={async () => { try { await settingsApi.createOrgUnit({ kind: uKind, code: uCode, nameEn: uName }); setUCode(''); setUName(''); await units.reload(); } catch (e) { toast.err(e); } }}>Add</Button>
              </Group>
            </Stack>
          </SimpleGrid>
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}
