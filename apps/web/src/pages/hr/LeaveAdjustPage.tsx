import { Button, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Table, Text, Textarea } from '@mantine/core';
import { IconCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { leave as leaveApi } from '../../api';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function LeaveAdjustPage() {
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  const types = useQuery(() => leaveApi.types(), []);
  const balances = useQuery(() => leaveApi.balances(employeeId!), [employeeId], { enabled: !!employeeId });
  const ledger = useQuery(() => leaveApi.ledger(employeeId!), [employeeId], { enabled: !!employeeId });
  const [typeId, setTypeId] = useState<string | null>(null); const [delta, setDelta] = useState<number | string>(1); const [kind, setKind] = useState('adjustment'); const [reason, setReason] = useState(''); const [date, setDate] = useState<string | null>(todayNpt());
  const [busy, setBusy] = useState(false);
  const post = async () => {
    setBusy(true);
    try { const r = await leaveApi.adjust({ employeeId, leaveTypeId: typeId, deltaDays: Number(delta), reason, kind, ...(date ? { effectiveDate: date } : {}) }); toast.ok(`Adjustment posted. New balance is ${r.balance.toFixed(2)} days.`); setReason(''); await Promise.all([balances.reload(), ledger.reload()]); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const balanceRows = balances.data?.balances ?? [];
  return (
    <Stack gap="lg">
      <PageHeader title="Leave adjustments" subtitle="Credit or debit leave days. Every change is recorded in the ledger; balances are never edited directly." />
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
        <Paper withBorder p="lg">
          <SectionTitle>New adjustment</SectionTitle>
          <Stack>
            <EmployeePicker value={employeeId} onChange={setEmployeeId} required />
            <Select label="Leave type" value={typeId} onChange={setTypeId} data={(types.data?.types ?? []).map((lt) => ({ value: lt.id, label: `${lt.name_en} (${lt.code})` }))} required />
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <NumberInput label="Days" description="Positive to credit, negative to debit" value={delta} onChange={setDelta} decimalScale={2} />
              <Select label="Kind" value={kind} onChange={(v) => v && setKind(v)} allowDeselect={false} data={[{ value: 'adjustment', label: 'Adjustment' }, { value: 'allocation', label: 'Allocation' }, { value: 'carry_forward', label: 'Carry forward' }]} />
            </SimpleGrid>
            <DateField label="Effective date" value={date} onChange={setDate} />
            <Textarea label="Reason" description="At least 3 characters; shown in the employee's ledger" required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
            <Group justify="flex-end">
              <Button leftSection={<IconCheck size={16} stroke={1.6} />} loading={busy} disabled={!employeeId || !typeId || !Number(delta) || reason.trim().length < 3} onClick={post}>Post adjustment</Button>
            </Group>
          </Stack>
        </Paper>
        <Paper withBorder p="lg">
          <SectionTitle hint={employeeId ? 'Current balance and what is available after pending requests.' : undefined}>Balances</SectionTitle>
          {!employeeId ? <EmptyState compact title="No employee selected" hint="Pick an employee to see their balances and ledger." />
            : balanceRows.length === 0 && !balances.loading ? <EmptyState compact title="No balances yet" hint="Post an allocation to give this employee leave." />
            : (
              <Table withRowBorders>
                <Table.Thead><Table.Tr><Table.Th>Leave</Table.Th><Table.Th style={{ textAlign: 'right' }}>Balance</Table.Th><Table.Th style={{ textAlign: 'right' }}>Available</Table.Th></Table.Tr></Table.Thead>
                <Table.Tbody>
                  {balanceRows.map((b) => (
                    <Table.Tr key={b.leaveTypeId}>
                      <Table.Td><Text size="sm" fw={500} ff="monospace">{b.code}</Text></Table.Td>
                      <Table.Td style={{ textAlign: 'right' }} className="num"><Text size="sm" fw={500}>{b.balance.toFixed(2)}</Text></Table.Td>
                      <Table.Td style={{ textAlign: 'right' }} className="num"><Text size="sm" c="dimmed">{b.available.toFixed(2)}</Text></Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
        </Paper>
      </SimpleGrid>
      {employeeId ? (
        <div>
          <SectionTitle hint="Every credit and debit for this employee, newest first.">Ledger</SectionTitle>
          <DataTable rows={(ledger.data?.ledger ?? []) as Array<Record<string, unknown>>} rowKey={(r) => String(r.id)} loading={ledger.loading} error={ledger.error} emptyTitle="No ledger entries yet" emptyHint="Entries appear here when leave is allocated, taken or adjusted."
            columns={[
              { key: 'effective_date', header: 'Date', render: (r) => <Text size="sm" className="num">{fmtAd(String(r.effective_date))}</Text> },
              { key: 'code', header: 'Leave', render: (r) => <Text size="sm" ff="monospace">{String(r.code)}</Text> },
              { key: 'delta_days', header: 'Days', align: 'right', render: (r) => { const v = Number(r.delta_days); return <Text size="sm" fw={500} className="num" c={v < 0 ? 'red' : v > 0 ? 'brand' : 'inherit'}>{v > 0 ? '+' : ''}{String(r.delta_days)}</Text>; } },
              { key: 'reason', header: 'Reason', render: (r) => <Text size="sm">{String(r.reason ?? '')}</Text> },
              { key: 'note', header: 'Note', render: (r) => <Text size="sm" c="dimmed">{String(r.note ?? '')}</Text> },
            ]} />
        </div>
      ) : null}
    </Stack>
  );
}
