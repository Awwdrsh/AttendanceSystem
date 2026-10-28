import { Button, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { BS_MONTH_NAMES_EN } from '@/lib/bs-calendar';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { bsMonthLabel, fmtMoney, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function LoansPage() {
  const [params] = useSearchParams();
  const [employeeId, setEmployeeId] = useState(params.get('employeeId'));
  const q = useQuery(() => payApi.loans(employeeId ?? undefined), [employeeId]);
  const bs = safeAdToBs(todayNpt());
  const [open, setOpen] = useState(false);
  const [emp, setEmp] = useState(null);
  const [kind, setKind] = useState('loan');
  const [principal, setPrincipal] = useState(0);
  const [emi, setEmi] = useState(0);
  const [y, setY] = useState(bs?.year ?? 2083);
  const [m, setM] = useState(bs?.month ?? 1);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const instalments = Number(principal) > 0 && Number(emi) > 0 ? Math.ceil(Number(principal) / Number(emi)) : null;
  const create = async () => {
    setBusy(true);
    try {
      const r = await payApi.createLoan({
        employeeId: emp,
        kind,
        principal: Number(principal).toFixed(2),
        emi: Number(emi).toFixed(2),
        startBsYear: Number(y),
        startBsMonth: Number(m),
        ...(note ? { note } : {}),
      });
      toast.ok(`${kind === 'advance' ? 'Advance' : 'Loan'} created with ${r.installments} instalments.`);
      setOpen(false);
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="lg">
      <PageHeader
        title="Loans & advances"
        subtitle="Monthly instalments are deducted on each payslip after statutory deductions (Labour Act, section 38)."
        actions={
          <>
            <EmployeePicker value={employeeId} onChange={setEmployeeId} label="" status="all" />
            <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setOpen(true)}>
              New loan or advance
            </Button>
          </>
        }
      />
      <DataTable
        rows={q.data?.loans ?? []}
        rowKey={(l) => l.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No loans or advances"
        emptyHint="When you lend money or pay salary in advance, record it here and the instalments come off payroll automatically."
        columns={[
          {
            key: 'emp_code',
            header: 'Employee',
            render: (l) => (
              <>
                <Text size="sm" fw={500}>
                  {l.emp_code}
                </Text>
                {l.note ? (
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    {l.note}
                  </Text>
                ) : null}
              </>
            ),
          },
          { key: 'kind', header: 'Type', render: (l) => (l.kind === 'advance' ? 'Advance' : l.kind === 'loan' ? 'Loan' : l.kind) },
          { key: 'principal', header: 'Principal', align: 'right', render: (l) => <span className="num">{fmtMoney(l.principal)}</span> },
          { key: 'emi', header: 'Monthly instalment', align: 'right', render: (l) => <span className="num">{fmtMoney(l.emi)}</span> },
          { key: 'start', header: 'Starts', render: (l) => bsMonthLabel(l.start_bs_year, l.start_bs_month) },
          {
            key: 'repaid',
            header: 'Repaid',
            align: 'right',
            render: (l) => (
              <>
                <Text size="sm" className="num">
                  {fmtMoney(l.repaid)}
                </Text>
                <Text size="xs" c="dimmed">
                  {l.installments} instalments
                </Text>
              </>
            ),
          },
          { key: 'status', header: 'Status', render: (l) => <StatusBadge status={l.status} /> },
        ]}
      />
      <Modal opened={open} onClose={() => setOpen(false)} title="New loan or advance" size="lg" centered>
        <Stack>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <EmployeePicker value={emp} onChange={setEmp} required />
            <Select
              label="Type"
              value={kind}
              onChange={(v) => v && setKind(v)}
              allowDeselect={false}
              data={[
                { value: 'loan', label: 'Loan' },
                { value: 'advance', label: 'Salary advance' },
              ]}
            />
            <NumberInput label="Principal (NPR)" value={principal} onChange={setPrincipal} min={1} thousandSeparator="," />
            <NumberInput
              label="Monthly instalment (NPR)"
              value={emi}
              onChange={setEmi}
              min={1}
              thousandSeparator=","
              description={instalments ? `${instalments} instalments` : 'Instalments = principal ÷ monthly amount, rounded up'}
            />
            <Select
              label="First deduction month"
              value={String(m)}
              onChange={(v) => v && setM(Number(v))}
              allowDeselect={false}
              data={BS_MONTH_NAMES_EN.map((name, i) => ({ value: String(i + 1), label: name }))}
            />
            <NumberInput label="Year" description="Nepali year, e.g. 2083" value={y} onChange={setY} />
          </SimpleGrid>
          <TextInput label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} placeholder="Optional" />
          <Group justify="flex-end" mt="xs">
            <Button variant="default" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={!emp || !Number(principal) || !Number(emi)} onClick={create}>
              Create
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
