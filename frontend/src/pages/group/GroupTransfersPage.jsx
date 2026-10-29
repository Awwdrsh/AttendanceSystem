import { Alert, Button, Checkbox, Group, Paper, Select, SimpleGrid, Stack, Switch, Text, TextInput, Textarea } from '@mantine/core';
import { IconArrowsExchange, IconInfoCircle } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { group as groupApi } from '../../api/group';
import { useAuth } from '../../auth/AuthProvider';
import { useCompanies } from '../../auth/CompaniesProvider';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { SwitchLink } from '../../components/group/SwitchLink';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDate, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const BLANK = {
  employeeId: null,
  toCompanyId: null,
  effectiveDate: null,
  empCode: '',
  designation: '',
  branchId: null,
  orgUnitId: null,
  managerId: null,
  shiftId: null,
  carryLeave: false,
  carrySalary: false,
  note: '',
};

const opts = (rows) => (rows ?? []).map((r) => ({ value: r.id, label: r.code ? `${r.name} (${r.code})` : r.name }));

function TransferRules({ canEdit }) {
  const q = useQuery(() => groupApi.transferRules(), []);
  return (
    <div>
      <SectionTitle hint="Whether people transferred into a company keep their leave balance. Set by group admins.">
        Leave on transfer
      </SectionTitle>
      <Paper withBorder p="md">
        <Stack gap="xs">
          {(q.data?.items ?? []).map((r) => (
            <Switch
              key={r.companyId}
              label={r.name}
              description={r.carryLeaveBalance ? 'Leave balances carry over' : 'Balances stay for the final settlement'}
              checked={r.carryLeaveBalance}
              disabled={!canEdit}
              onChange={async (e) => {
                const on = e.currentTarget.checked;
                try {
                  await groupApi.setTransferRule(r.companyId, on);
                  q.setData((d) => ({
                    items: d.items.map((x) => (x.companyId === r.companyId ? { ...x, carryLeaveBalance: on } : x)),
                  }));
                } catch (ex) {
                  toast.err(ex);
                }
              }}
            />
          ))}
          {q.error ? <Text size="sm" c="red">{q.error}</Text> : null}
        </Stack>
      </Paper>
    </div>
  );
}

export default function GroupTransfersPage() {
  const { hasRole } = useAuth();
  const { active, access } = useCompanies();
  const targets = useQuery(() => groupApi.transferTargets(), []);
  const history = useQuery(() => groupApi.transfers(), []);
  const [f, setF] = useState(BLANK);
  const [confirm, setConfirm] = useState(false);
  const [employeeName, setEmployeeName] = useState('');
  const [result, setResult] = useState(null);
  const options = useQuery(
    () => (f.toCompanyId ? groupApi.transferTarget(f.toCompanyId) : Promise.resolve(null)),
    [f.toCompanyId],
  );
  useEffect(() => {
    setF((x) => ({ ...x, branchId: null, orgUnitId: null, managerId: null, shiftId: null, carryLeave: false }));
  }, [f.toCompanyId]);
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v?.currentTarget ? v.currentTarget.value : v }));
  const target = (targets.data?.items ?? []).find((t) => t.id === f.toCompanyId);
  const o = options.data;
  const ready = f.employeeId && f.toCompanyId && f.effectiveDate;
  const today = todayNpt();
  const canHere = hasRole('hr');

  const submit = async () => {
    const body = {
      employeeId: f.employeeId,
      toCompanyId: f.toCompanyId,
      effectiveDate: f.effectiveDate,
      carryLeave: f.carryLeave,
      carrySalary: f.carrySalary,
    };
    for (const k of ['empCode', 'designation', 'note']) if (f[k].trim()) body[k] = f[k].trim();
    for (const k of ['branchId', 'orgUnitId', 'managerId', 'shiftId']) if (f[k]) body[k] = f[k];
    try {
      const r = await groupApi.transfer(body);
      setResult(r);
      setF(BLANK);
      void history.reload();
      toast.ok(`${r.name} moves to ${r.toCompany} from ${fmtDate(r.effectiveDate)}.`, 'Transfer recorded');
    } catch (e) {
      toast.err(e, 'Not transferred');
    }
  };

  return (
    <Stack gap="lg">
      <PageHeader
        title="Transfers"
        subtitle={`Move an employee of ${active?.name ?? 'this company'} to a sister company. Their employment here ends the day before through the usual exit; a new record starts there with their history.`}
      />
      {!canHere ? (
        <Alert icon={<IconInfoCircle size={18} />} color="blue" variant="light">
          You can transfer people out of a company where you are HR. Switch to the company the employee works in first.
        </Alert>
      ) : (targets.data?.items ?? []).length === 0 && !targets.loading ? (
        <EmptyState
          title="No sister company to transfer to"
          hint={targets.error ?? 'You need the HR role in at least one other company of the group.'}
        />
      ) : (
        <Paper withBorder p="lg">
          <Stack gap="sm">
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
              <EmployeePicker
                value={f.employeeId}
                onChange={(v, e) => {
                  set('employeeId')(v);
                  setEmployeeName(e?.name_en ?? '');
                  if (e?.designation && !f.designation) setF((x) => ({ ...x, designation: e.designation }));
                }}
                required
              />
              <Select
                label="To company"
                placeholder="Choose a sister company"
                data={(targets.data?.items ?? []).map((t) => ({ value: t.id, label: `${t.name} (${t.code})` }))}
                value={f.toCompanyId}
                onChange={set('toCompanyId')}
                required
              />
              <DateField
                label="First day in the new company"
                description="The last working day here is the day before."
                value={f.effectiveDate}
                onChange={set('effectiveDate')}
                minDate={addDays(today, -31)}
                maxDate={addDays(today, 365)}
                required
              />
              <TextInput
                label="Employee code there"
                description="Leave empty to keep the current code"
                value={f.empCode}
                onChange={set('empCode')}
              />
              <TextInput label="Designation there" value={f.designation} onChange={set('designation')} />
              <Select label="Branch" placeholder="None" clearable searchable data={opts(o?.branches)} value={f.branchId} onChange={set('branchId')} disabled={!o} />
              <Select label="Department" placeholder="None" clearable searchable data={opts(o?.orgUnits)} value={f.orgUnitId} onChange={set('orgUnitId')} disabled={!o} />
              <Select label="Manager" placeholder="None" clearable searchable data={opts(o?.managers)} value={f.managerId} onChange={set('managerId')} disabled={!o} />
              <Select label="Shift" placeholder="Company default" clearable data={opts(o?.shifts)} value={f.shiftId} onChange={set('shiftId')} disabled={!o} />
            </SimpleGrid>
            <Checkbox
              label="Carry the leave balance over"
              description={
                target && !target.carryLeaveBalance
                  ? `${target.name} does not carry leave balances over; the balance stays here for the final settlement.`
                  : 'The balance moves by leave type code and is reduced here by the same days.'
              }
              checked={f.carryLeave}
              disabled={!target?.carryLeaveBalance}
              onChange={(e) => set('carryLeave')(e.currentTarget.checked)}
            />
            <Checkbox
              label="Carry the salary over"
              description="Only when the new company has a salary structure with the same code."
              checked={f.carrySalary}
              onChange={(e) => set('carrySalary')(e.currentTarget.checked)}
            />
            <Textarea label="Note" autosize minRows={2} maxLength={500} value={f.note} onChange={set('note')} />
            <Group justify="flex-end">
              <Button leftSection={<IconArrowsExchange size={16} stroke={1.6} />} disabled={!ready} onClick={() => setConfirm(true)}>
                Transfer
              </Button>
            </Group>
          </Stack>
        </Paper>
      )}

      {result ? (
        <Alert color="teal" variant="light" title={`${result.name} transferred`} withCloseButton onClose={() => setResult(null)}>
          <Stack gap={4}>
            <Text size="sm">
              Last working day at {result.fromCompany}: {fmtDate(result.lastWorkingDay)}. First day at {result.toCompany}:{' '}
              {fmtDate(result.effectiveDate)} as {result.toEmpCode}.
            </Text>
            <Text size="sm">
              {result.historyCopied} career events copied.{' '}
              {result.leaveCarried?.length
                ? `Leave carried: ${result.leaveCarried.map((l) => `${l.days} ${l.name}`).join(', ')}.`
                : 'No leave carried.'}{' '}
              {result.salaryCarried ? 'Salary carried over.' : result.salaryNote ?? ''}
            </Text>
            <SwitchLink companyId={result.toCompanyId} to={`/employees/${result.toEmployeeId}`}>
              Open the new record in {result.toCompany}
            </SwitchLink>
          </Stack>
        </Alert>
      ) : null}

      <div>
        <SectionTitle hint="Into or out of the companies where you are HR">Recent transfers</SectionTitle>
        <DataTable
          rows={history.data?.items ?? []}
          rowKey={(r) => `${r.toEmployeeId}`}
          loading={history.loading}
          error={history.error}
          emptyTitle="No transfers yet."
          columns={[
            { key: 'name', header: 'Employee' },
            { key: 'from', header: 'From', render: (r) => `${r.fromCompany} (${r.fromEmpCode})` },
            { key: 'to', header: 'To', render: (r) => `${r.toCompany} (${r.toEmpCode})` },
            { key: 'effectiveDate', header: 'First day there', render: (r) => fmtDate(r.effectiveDate) },
            {
              key: 'recordedAt',
              header: 'Recorded',
              render: (r) => `${relativeTime(r.recordedAt)}${r.recordedBy ? ` by ${r.recordedBy}` : ''}`,
            },
          ]}
        />
      </div>

      <TransferRules canEdit={access.admin} />

      <ConfirmDialog
        opened={confirm}
        title="Transfer this employee?"
        color="brand"
        message={`${employeeName || 'The employee'} leaves ${active?.name ?? 'this company'} on ${
          f.effectiveDate ? fmtDate(addDays(f.effectiveDate, -1)) : '—'
        } and starts at ${target?.name ?? 'the sister company'} on ${f.effectiveDate ? fmtDate(f.effectiveDate) : '—'}. Their account follows them.`}
        confirmLabel="Transfer"
        onClose={() => setConfirm(false)}
        onConfirm={submit}
      />
    </Stack>
  );
}
