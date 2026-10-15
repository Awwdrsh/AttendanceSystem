import { Badge, Button, Group, Modal, Paper, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput } from '@mantine/core';
import { IconDownload, IconPlus } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { expenses as api, type Destination, type ExpenseCategory, type ExpensePolicy, type TadaRate } from '../../api/expenses';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { DEST_LABEL, isAmount, money } from './shared';

const DESTS: Destination[] = ['valley', 'city', 'rural', 'international'];
const clean = (s: string) => s.replace(/[^\d.]/g, '');

function SettingsCard({ policy, onSaved }: { policy: ExpensePolicy; onSaved: () => void }) {
  const [rate, setRate] = useState(policy.settings.mileageRatePerKm);
  const [mode, setMode] = useState(policy.settings.limitMode);
  const [days, setDays] = useState(String(policy.settings.claimWindowDays));
  const [busy, setBusy] = useState(false);
  const ok = isAmount(rate) && Number(days) >= 7 && Number(days) <= 365;
  const save = async () => {
    setBusy(true);
    try { await api.saveSettings({ mileageRatePerKm: rate, limitMode: mode, claimWindowDays: Number(days) }); toast.ok('Expense rules saved'); onSaved(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Paper withBorder radius="lg" p="md">
      <Stack gap="sm">
        <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm">
          <TextInput label="Own vehicle rate (NPR per km)" inputMode="decimal" value={rate} onChange={(e) => setRate(clean(e.currentTarget.value))} error={rate && !isAmount(rate) ? 'Up to 2 decimals' : undefined} />
          <TextInput label="Claims allowed for (days after spending)" inputMode="numeric" value={days} onChange={(e) => setDays(e.currentTarget.value.replace(/\D/g, ''))} error={days && (Number(days) < 7 || Number(days) > 365) ? '7 to 365 days' : undefined} />
          <Stack gap={4}>
            <Text size="sm" fw={500}>When a limit is exceeded</Text>
            <SegmentedControl value={mode} onChange={(v) => setMode(v as 'block' | 'warn')} data={[{ value: 'block', label: 'Block the claim' }, { value: 'warn', label: 'Warn the approver' }]} />
          </Stack>
        </SimpleGrid>
        <Group justify="flex-end"><Button onClick={save} loading={busy} disabled={!ok}>Save rules</Button></Group>
      </Stack>
    </Paper>
  );
}

interface LimitRow { grade: string; perClaim: string; perMonth: string }

function CategoryModal({ policy, category, onClose, onSaved }: { policy: ExpensePolicy; category: ExpenseCategory | 'new' | null; onClose: () => void; onSaved: () => void }) {
  const isNew = category === 'new';
  const c = category && category !== 'new' ? category : null;
  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [kind, setKind] = useState<'amount' | 'mileage'>('amount');
  const [receipt, setReceipt] = useState('');
  const [active, setActive] = useState(true);
  const [limits, setLimits] = useState<LimitRow[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setCode(''); setName(c?.name ?? ''); setKind(c?.kind ?? 'amount'); setReceipt(c?.receiptRequiredAbove ?? ''); setActive(c?.active ?? true);
    setLimits(c ? policy.limits.filter((l) => l.categoryId === c.id).map((l) => ({ grade: l.grade, perClaim: l.perClaim ?? '', perMonth: l.perMonth ?? '' })) : []);
  }, [category]); // eslint-disable-line react-hooks/exhaustive-deps
  const grades = [...new Set(['*', ...policy.grades, ...limits.map((l) => l.grade)])];
  const limitsOk = limits.every((l) => l.grade && (!l.perClaim || isAmount(l.perClaim)) && (!l.perMonth || isAmount(l.perMonth)));
  const ok = name.trim() && (!isNew || /^[A-Z][A-Z0-9_]{1,29}$/.test(code)) && (!receipt || isAmount(receipt)) && limitsOk && new Set(limits.map((l) => l.grade)).size === limits.length;
  const save = async () => {
    setBusy(true);
    try {
      let id = c?.id;
      const receiptRequiredAbove = kind === 'mileage' ? null : receipt || null;
      if (isNew) id = (await api.addCategory({ code, name: name.trim(), kind, receiptRequiredAbove })).id;
      else await api.updateCategory(id!, { name: name.trim(), kind, receiptRequiredAbove, active });
      await api.setLimits(id!, limits.map((l) => ({ grade: l.grade, perClaim: l.perClaim || null, perMonth: l.perMonth || null })));
      toast.ok('Category saved'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={!!category} onClose={onClose} title={<Text fw={600}>{isNew ? 'New category' : c?.name}</Text>} size="lg" centered>
      <Stack gap="sm">
        <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
          {isNew ? <TextInput label="Code" description="Capital letters, digits and _" value={code} onChange={(e) => setCode(e.currentTarget.value.toUpperCase())} maxLength={30} /> : null}
          <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={80} />
          <Select label="Claimed as" data={[{ value: 'amount', label: 'An amount' }, { value: 'mileage', label: 'Distance (km × rate)' }]} value={kind} onChange={(v) => v && setKind(v as 'amount' | 'mileage')} allowDeselect={false} />
          {kind === 'amount' ? <TextInput label="Receipt needed above (NPR)" description="0 = always; empty = never" inputMode="decimal" value={receipt} onChange={(e) => setReceipt(clean(e.currentTarget.value))} /> : null}
        </SimpleGrid>
        {!isNew ? <Switch label="Employees can choose this category" checked={active} onChange={(e) => setActive(e.currentTarget.checked)} /> : null}
        <Text size="sm" fw={600} mt="xs">Limits by grade</Text>
        <Text size="xs" c="dimmed">Grade * applies to every grade without its own row. Leave a limit empty for no limit.</Text>
        {limits.map((l, i) => (
          <Group key={i} gap="xs" align="flex-end" wrap="wrap">
            <Select label={i === 0 ? 'Grade' : undefined} data={grades.map((g) => ({ value: g, label: g === '*' ? 'Every grade (*)' : g }))} value={l.grade} searchable
              onChange={(v) => v && setLimits((ls) => ls.map((x, k) => (k === i ? { ...x, grade: v } : x)))} w={170} allowDeselect={false} />
            <TextInput label={i === 0 ? 'Per claim' : undefined} inputMode="decimal" value={l.perClaim} onChange={(e) => { const v = clean(e.currentTarget.value); setLimits((ls) => ls.map((x, k) => (k === i ? { ...x, perClaim: v } : x))); }} w={130} />
            <TextInput label={i === 0 ? 'Per month' : undefined} inputMode="decimal" value={l.perMonth} onChange={(e) => { const v = clean(e.currentTarget.value); setLimits((ls) => ls.map((x, k) => (k === i ? { ...x, perMonth: v } : x))); }} w={130} />
            <Button variant="subtle" color="red" size="compact-sm" onClick={() => setLimits((ls) => ls.filter((_, k) => k !== i))}>Remove</Button>
          </Group>
        ))}
        <Button variant="subtle" leftSection={<IconPlus size={14} />} w="fit-content" size="compact-sm" onClick={() => setLimits((ls) => [...ls, { grade: grades.find((g) => !ls.some((x) => x.grade === g)) ?? '*', perClaim: '', perMonth: '' }])}>Add a limit</Button>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button onClick={save} loading={busy} disabled={!ok}>Save</Button></Group>
      </Stack>
    </Modal>
  );
}

function TadaCard({ policy, onSaved }: { policy: ExpensePolicy; onSaved: () => void }) {
  const [rates, setRates] = useState<Record<string, { daRate: string; lodgingRate: string }>>({});
  const [grades, setGrades] = useState<string[]>([]);
  const [newGrade, setNewGrade] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const m: typeof rates = {};
    for (const r of policy.tadaRates) m[`${r.grade}|${r.destination}`] = { daRate: r.daRate, lodgingRate: r.lodgingRate };
    setRates(m);
    setGrades([...new Set(['*', ...policy.tadaRates.map((r) => r.grade)])]);
  }, [policy]);
  const setCell = (k: string, f: 'daRate' | 'lodgingRate', v: string) => setRates((r) => ({ ...r, [k]: { daRate: r[k]?.daRate ?? '', lodgingRate: r[k]?.lodgingRate ?? '', [f]: clean(v) } }));
  const out: TadaRate[] = [];
  let ok = true;
  for (const g of grades) for (const d of DESTS) {
    const v = rates[`${g}|${d}`];
    if (!v || (!v.daRate && !v.lodgingRate)) continue;
    if (!isAmount(v.daRate || '0') || !isAmount(v.lodgingRate || '0')) ok = false;
    out.push({ grade: g, destination: d, daRate: v.daRate || '0', lodgingRate: v.lodgingRate || '0' });
  }
  const save = async () => {
    setBusy(true);
    try { await api.setTadaRates(out); toast.ok('Travel rates saved'); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const addable = policy.grades.filter((g) => !grades.includes(g));
  return (
    <Paper withBorder radius="lg" p="md">
      <Stack gap="sm">
        <Table.ScrollContainer minWidth={720}>
          <Table verticalSpacing={6}>
            <Table.Thead><Table.Tr><Table.Th>Grade</Table.Th>{DESTS.map((d) => <Table.Th key={d}>{DEST_LABEL[d]}{d === 'international' ? ' (USD)' : ''}</Table.Th>)}</Table.Tr></Table.Thead>
            <Table.Tbody>
              {grades.map((g) => (
                <Table.Tr key={g}>
                  <Table.Td><Text size="sm" fw={500}>{g === '*' ? 'Every grade' : g}</Text></Table.Td>
                  {DESTS.map((d) => {
                    const k = `${g}|${d}`;
                    return (
                      <Table.Td key={d}>
                        <Group gap={4} wrap="nowrap">
                          <TextInput size="xs" w={80} aria-label="Daily allowance" placeholder="Daily" value={rates[k]?.daRate ?? ''} onChange={(e) => setCell(k, 'daRate', e.currentTarget.value)} />
                          <TextInput size="xs" w={80} aria-label="Lodging per night" placeholder="Night" value={rates[k]?.lodgingRate ?? ''} onChange={(e) => setCell(k, 'lodgingRate', e.currentTarget.value)} />
                        </Group>
                      </Table.Td>
                    );
                  })}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Text size="xs" c="dimmed">Each cell: daily allowance, then lodging limit per night. International rates are US dollars, converted at the rate entered on the trip.</Text>
        <Group justify="space-between" wrap="wrap">
          <Group gap="xs">
            <Select placeholder={addable.length ? 'Add a grade' : 'All grades listed'} data={addable} value={newGrade} onChange={setNewGrade} disabled={!addable.length} w={160} />
            <Button variant="default" disabled={!newGrade} onClick={() => { if (newGrade) { setGrades((g) => [...g, newGrade]); setNewGrade(null); } }}>Add</Button>
          </Group>
          <Button onClick={save} loading={busy} disabled={!ok}>Save travel rates</Button>
        </Group>
      </Stack>
    </Paper>
  );
}

function SpendReport() {
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -90));
  const [to, setTo] = useState<string | null>(todayNpt());
  const [groupBy, setGroupBy] = useState<'category' | 'department' | 'month' | 'employee'>('category');
  const q = useQuery(() => (from && to ? api.spend({ from, to, groupBy }) : Promise.resolve(null)), [from, to, groupBy]);
  const rows = q.data?.rows ?? [];
  const total = rows.reduce((a, r) => a + Number(r.amount), 0);
  return (
    <Stack gap="sm">
      <SectionTitle hint="Approved claims and settled trips."
        actions={<Group gap="xs" wrap="wrap">
          <SegmentedControl size="xs" value={groupBy} onChange={(v) => setGroupBy(v as typeof groupBy)} data={[{ value: 'category', label: 'Category' }, { value: 'department', label: 'Department' }, { value: 'month', label: 'Month' }, { value: 'employee', label: 'Employee' }]} />
          <DateField value={from} onChange={setFrom} maxDate={to ?? todayNpt()} w={170} />
          <DateField value={to} onChange={setTo} {...(from ? { minDate: from } : {})} w={170} />
          <Button variant="default" size="xs" leftSection={<IconDownload size={14} />} disabled={!from || !to}
            onClick={() => api.downloadReport('spend', { from: from!, to: to!, groupBy }).catch((e) => toast.err(e, 'Download failed'))}>CSV</Button>
        </Group>}>Spending</SectionTitle>
      <DataTable rows={rows} rowKey={(r) => r.group} loading={q.loading} error={q.error} emptyTitle="Nothing spent in this period" columns={[
        { key: 'g', header: groupBy.charAt(0).toUpperCase() + groupBy.slice(1), render: (r) => r.group },
        { key: 'i', header: 'Items', align: 'right', render: (r) => r.items },
        { key: 'a', header: 'Amount', align: 'right', render: (r) => money(r.amount) },
        { key: 's', header: 'Share', align: 'right', render: (r) => (total ? `${Math.round((Number(r.amount) / total) * 100)}%` : '—') },
      ]} />
      {rows.length ? <Text size="sm" ta="right">Total <Text span fw={700}>{money(total)}</Text></Text> : null}
    </Stack>
  );
}

/** HR: expense categories with limits by grade, travel allowance rates, and spending. */
export default function ExpensePoliciesPage() {
  const q = useQuery(() => api.policy(), []);
  const [edit, setEdit] = useState<ExpenseCategory | 'new' | null>(null);
  const p = q.data;
  const limitText = (c: ExpenseCategory) => {
    const ls = p?.limits.filter((l) => l.categoryId === c.id) ?? [];
    if (!ls.length) return 'No limit';
    return ls.map((l) => `${l.grade === '*' ? 'All' : l.grade}: ${[l.perClaim ? `${money(l.perClaim)}/claim` : '', l.perMonth ? `${money(l.perMonth)}/month` : ''].filter(Boolean).join(', ')}`).join(' · ');
  };
  return (
    <Stack gap="xl">
      {p ? (
        <>
          <Stack gap="sm">
            <SectionTitle hint="How claims are checked before they reach an approver.">Claim rules</SectionTitle>
            <SettingsCard policy={p} onSaved={() => void q.reload()} />
          </Stack>
          <Stack gap="sm">
            <SectionTitle hint="What employees can claim, when a receipt is needed, and limits by grade."
              actions={<Button variant="default" leftSection={<IconPlus size={16} />} onClick={() => setEdit('new')}>Add category</Button>}>Expense categories</SectionTitle>
            <DataTable<ExpenseCategory> rows={p.categories} rowKey={(c) => c.id} onRowClick={setEdit} columns={[
              { key: 'n', header: 'Category', render: (c) => <><Group gap={6}><Text size="sm" fw={500}>{c.name}</Text>{!c.active ? <Badge size="xs" color="gray" variant="light">Hidden</Badge> : null}</Group><Text size="xs" c="dimmed" ff="monospace">{c.code}</Text></> },
              { key: 'k', header: 'Claimed as', render: (c) => (c.kind === 'mileage' ? `km × ${money(p.settings.mileageRatePerKm)}` : 'Amount') },
              { key: 'r', header: 'Receipt', render: (c) => (c.receiptRequiredAbove === null ? 'Not needed' : Number(c.receiptRequiredAbove) > 0 ? `Above ${money(c.receiptRequiredAbove)}` : 'Always') },
              { key: 'l', header: 'Limits', render: (c) => <Text size="sm" c="dimmed">{limitText(c)}</Text> },
            ]} />
          </Stack>
          <Stack gap="sm">
            <SectionTitle hint="Daily allowance and lodging per night by grade and destination. Trips use the grade's row, or Every grade.">Travel allowance (TADA)</SectionTitle>
            <TadaCard policy={p} onSaved={() => void q.reload()} />
          </Stack>
        </>
      ) : <DataTable rows={[]} rowKey={() => ''} loading={q.loading} error={q.error} columns={[]} />}
      <SpendReport />
      {p ? <CategoryModal policy={p} category={edit} onClose={() => setEdit(null)} onSaved={() => void q.reload()} /> : null}
    </Stack>
  );
}
