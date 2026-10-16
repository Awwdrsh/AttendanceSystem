import { ActionIcon, Alert, Anchor, Badge, Button, Divider, Drawer, Group, Loader, NumberInput, Paper, Select, SimpleGrid, Stack, Table, Text, Textarea, TextInput } from '@mantine/core';
import { IconAlertTriangle, IconPaperclip, IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  expenses as api, type CheckIssue, type ClaimDetail, type ClaimItemInput, type ClaimStatus, type Destination, type ExpensePolicy, type Payout, type TadaQuote,
  type TravelBill, type TravelMode, type TripDetail, type TripStatus,
} from '../../api/expenses';
import type { UploadedFile } from '../../api/files';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { FileUpload } from '../../components/FileUpload';
import { Field } from '../../components/Kpi';
import { addDays, bsMonthLabel, fmtAd, fmtDateShort, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ApprovalChainPreview } from '../../components/ApprovalChainPreview';

// ---------------------------------------------------------------- labels
export const CLAIM_STATUS: Record<ClaimStatus, { label: string; color: string }> = {
  draft: { label: 'Draft', color: 'gray' }, pending: { label: 'Waiting for approval', color: 'yellow' }, approved: { label: 'Approved', color: 'brand' },
  rejected: { label: 'Rejected', color: 'red' }, cancelled: { label: 'Cancelled', color: 'gray' }, paid: { label: 'Paid', color: 'teal' },
};
export const TRIP_STATUS: Record<TripStatus, { label: string; color: string }> = {
  pending: { label: 'Waiting for approval', color: 'yellow' }, approved: { label: 'Approved', color: 'brand' }, rejected: { label: 'Rejected', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' }, settling: { label: 'Settlement submitted', color: 'blue' }, settled: { label: 'Settled', color: 'teal' },
};
export const DEST_LABEL: Record<Destination, string> = { valley: 'Kathmandu valley', city: 'Other city', rural: 'Rural area', international: 'International' };
export const MODE_LABEL: Record<TravelMode, string> = { bus: 'Bus', air: 'Air', office_vehicle: 'Office vehicle', own_vehicle: 'Own vehicle', other: 'Other' };
export const BILL_KIND: Record<TravelBill['kind'], string> = { transport: 'Transport', lodging: 'Lodging', other: 'Other' };

export const money = (v: string | number | null | undefined) => fmtMoney(v, { decimals: Number(v ?? 0) % 1 === 0 ? 0 : 2 });
export const isAmount = (s: string) => /^\d{1,10}(\.\d{1,2})?$/.test(s.trim());
export const span = (from: string, to: string) => (from === to ? fmtAd(from) : `${fmtDateShort(from)} to ${fmtAd(to)}`);

export function ClaimBadge({ status }: { status: ClaimStatus }) {
  const s = CLAIM_STATUS[status];
  return <Badge variant="light" color={s.color}>{s.label}</Badge>;
}
export function TripBadge({ status }: { status: TripStatus }) {
  const s = TRIP_STATUS[status];
  return <Badge variant="light" color={s.color}>{s.label}</Badge>;
}

/** Where the money stands: "In Ashwin 2083 salary", "Paid in cash", "Next salary". */
export function payoutLabel(p: Pick<Payout, 'status' | 'bsYear' | 'bsMonth' | 'code'>): string {
  const verb = p.code === 'EXP_RECOVERY' ? 'Recovered' : 'Paid';
  if (p.status === 'cash') return `${verb} outside payroll`;
  if (p.status === 'cancelled') return 'Cancelled';
  if (p.status === 'paid') return p.bsYear ? `${verb} in ${bsMonthLabel(p.bsYear, p.bsMonth ?? 1)} salary` : verb;
  return p.bsYear ? `In the ${bsMonthLabel(p.bsYear, p.bsMonth ?? 1)} pay run` : p.code === 'EXP_RECOVERY' ? 'To be recovered from the next salary' : 'With the next salary';
}

function Issues({ issues }: { issues: CheckIssue[] }) {
  if (!issues.length) return null;
  const blocks = issues.filter((i) => i.level === 'block');
  return (
    <Alert variant="light" color={blocks.length ? 'red' : 'yellow'} icon={<IconAlertTriangle size={18} stroke={1.6} />} title={blocks.length ? 'Fix before submitting' : 'Over policy'}>
      <Stack gap={2}>{issues.map((i, n) => <Text key={n} size="sm">{i.message}</Text>)}</Stack>
    </Alert>
  );
}

// ---------------------------------------------------------------- claim form (employee)
interface LineDraft { key: number; expenseDate: string | null; categoryId: string | null; description: string; amount: string; km: number | string; receipt: UploadedFile | null }
let lineSeq = 1;
const blankLine = (): LineDraft => ({ key: lineSeq++, expenseDate: todayNpt(), categoryId: null, description: '', amount: '', km: '', receipt: null });

/** New or draft claim: lines with live limit and receipt checks; save as draft or submit. */
export function ClaimForm({ policy, claim, onDone }: { policy: ExpensePolicy; claim?: ClaimDetail | null; onDone: (id: string) => void }) {
  const [title, setTitle] = useState(claim?.claim.title ?? '');
  const [lines, setLines] = useState<LineDraft[]>(() => claim?.items.length
    ? claim.items.map((i) => ({ key: lineSeq++, expenseDate: i.expenseDate, categoryId: i.categoryId, description: i.description, amount: i.categoryKind === 'mileage' ? '' : i.amount,
        km: i.km ? Number(i.km) : '', receipt: i.receiptFileId ? { id: i.receiptFileId, filename: i.receiptName ?? 'Receipt', mime: i.receiptMime ?? '', bytes: 0, purpose: 'expense_receipt' } : null }))
    : [blankLine()]);
  const [check, setCheck] = useState<{ amounts: string[]; total: string; issues: CheckIssue[] } | null>(null);
  const [busy, setBusy] = useState<'draft' | 'submit' | null>(null);
  const cats = policy.categories.filter((c) => c.active || lines.some((l) => l.categoryId === c.id));
  const catById = useMemo(() => new Map(policy.categories.map((c) => [c.id, c])), [policy.categories]);

  const items: ClaimItemInput[] | null = useMemo(() => {
    const out: ClaimItemInput[] = [];
    for (const l of lines) {
      const c = l.categoryId ? catById.get(l.categoryId) : null;
      if (!l.expenseDate || !c || !l.description.trim()) return null;
      if (c.kind === 'mileage') { if (!(Number(l.km) > 0)) return null; out.push({ expenseDate: l.expenseDate, categoryId: c.id, description: l.description.trim(), km: Number(l.km), receiptFileId: l.receipt?.id ?? null }); }
      else { if (!isAmount(l.amount) || !(Number(l.amount) > 0)) return null; out.push({ expenseDate: l.expenseDate, categoryId: c.id, description: l.description.trim(), amount: l.amount.trim(), receiptFileId: l.receipt?.id ?? null }); }
    }
    return out;
  }, [lines, catById]);

  useEffect(() => {
    if (!items) { setCheck(null); return; }
    const t = setTimeout(() => { api.check(items, claim?.claim.id).then(setCheck).catch(() => setCheck(null)); }, 350);
    return () => clearTimeout(t);
  }, [JSON.stringify(items), claim?.claim.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (key: number, patch: Partial<LineDraft>) => setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  const blocked = (check?.issues ?? []).some((i) => i.level === 'block');
  const save = async (submit: boolean) => {
    if (!items || !title.trim()) return;
    setBusy(submit ? 'submit' : 'draft');
    try {
      const body = { title: title.trim(), items, submit };
      const r = claim ? await api.updateClaim(claim.claim.id, body) : await api.createClaim(body);
      toast.ok(submit ? 'Your manager has been asked to approve it.' : 'Saved as a draft. Submit it when it is complete.', submit ? 'Claim submitted' : 'Draft saved');
      onDone(r.id);
    } catch (e) { toast.err(e, submit ? 'Not submitted' : 'Not saved'); } finally { setBusy(null); }
  };

  return (
    <Stack gap="md">
      <TextInput label="What is this claim for?" placeholder="e.g. Client visit to Lalitpur" value={title} onChange={(e) => setTitle(e.currentTarget.value)} maxLength={120} required />
      {lines.map((l, n) => {
        const c = l.categoryId ? catById.get(l.categoryId) : null;
        const lineIssues = (check?.issues ?? []).filter((i) => i.item === n);
        const needsReceipt = c && c.receiptRequiredAbove !== null && c.kind === 'amount';
        return (
          <Paper key={l.key} withBorder radius="md" p="sm">
            <Stack gap="xs">
              <Group justify="space-between" wrap="nowrap">
                <Text size="sm" fw={600}>Expense {n + 1}</Text>
                {lines.length > 1 ? <ActionIcon variant="subtle" color="gray" aria-label="Remove line" onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}><IconTrash size={16} stroke={1.6} /></ActionIcon> : null}
              </Group>
              <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="xs">
                <DateField label="Date" value={l.expenseDate} onChange={(v) => set(l.key, { expenseDate: v })} maxDate={todayNpt()} minDate={addDays(todayNpt(), -policy.settings.claimWindowDays)} required />
                <Select label="Category" data={cats.map((x) => ({ value: x.id, label: x.name }))} value={l.categoryId} onChange={(v) => set(l.key, { categoryId: v })} required allowDeselect={false} />
              </SimpleGrid>
              <TextInput label="Description" placeholder="What and where" value={l.description} onChange={(e) => set(l.key, { description: e.currentTarget.value })} maxLength={300} required />
              <Group align="flex-end" gap="sm" wrap="wrap">
                {c?.kind === 'mileage' ? (
                  <NumberInput label="Distance (km)" min={0.1} max={5000} decimalScale={1} value={l.km} onChange={(v) => set(l.key, { km: v })} w={160}
                    description={`${fmtMoney(policy.settings.mileageRatePerKm, { decimals: 2 })} per km${Number(l.km) > 0 ? ` = ${money(Number(l.km) * Number(policy.settings.mileageRatePerKm))}` : ''}`} />
                ) : (
                  <TextInput label="Amount (NPR)" inputMode="decimal" placeholder="0" value={l.amount} onChange={(e) => set(l.key, { amount: e.currentTarget.value.replace(/[^\d.]/g, '') })} w={160}
                    error={l.amount && !isAmount(l.amount) ? 'Up to 2 decimals' : undefined} />
                )}
                <Stack gap={4}>
                  <Text size="xs" c="dimmed">{needsReceipt ? (Number(c.receiptRequiredAbove) > 0 ? `Receipt needed above ${money(c.receiptRequiredAbove)}` : 'Receipt needed') : 'Receipt (optional)'}</Text>
                  <FileUpload purpose="expense_receipt" value={l.receipt} onChange={(f) => set(l.key, { receipt: f })} label="Attach receipt" />
                </Stack>
              </Group>
              {lineIssues.map((i, k) => <Text key={k} size="xs" c={i.level === 'block' ? 'red' : 'orange'}>{i.message}</Text>)}
            </Stack>
          </Paper>
        );
      })}
      <Button variant="subtle" leftSection={<IconPlus size={16} />} onClick={() => setLines((ls) => [...ls, blankLine()])} w="fit-content" disabled={lines.length >= 50}>Add another expense</Button>
      <Issues issues={(check?.issues ?? []).filter((i) => i.item === undefined)} />
      <ApprovalChainPreview objectType="expense_claim" amount={check ? Number(check.total) : null} />
      <Group justify="space-between" wrap="wrap">
        <Text size="sm">Total <Text span fw={700}>{check ? money(check.total) : '—'}</Text></Text>
        <Group gap="xs">
          <Button variant="default" onClick={() => save(false)} loading={busy === 'draft'} disabled={!items || !title.trim() || !!busy}>Save draft</Button>
          <Button onClick={() => save(true)} loading={busy === 'submit'} disabled={!items || !title.trim() || blocked || !!busy}>Submit for approval</Button>
        </Group>
      </Group>
    </Stack>
  );
}

// ---------------------------------------------------------------- claim detail and review
/** Claim with its lines and receipts. Approvers can lower a line (with a reason), approve or reject; HR can record payment outside payroll. */
export function ClaimDrawer({ id, onClose, onChanged, onEdit }: { id: string | null; onClose: () => void; onChanged: () => void; onEdit?: (d: ClaimDetail) => void }) {
  const { user, hasRole } = useAuth();
  const q = useQuery(() => (id ? api.claim(id) : Promise.resolve(null)), [id]);
  const d = q.data;
  const [adjust, setAdjust] = useState<Record<string, { amount: string; reason: string }>>({});
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [cashNote, setCashNote] = useState('');
  useEffect(() => { setAdjust({}); setReason(''); setCashNote(''); }, [id]);
  const mine = d?.claim.employee.id === user?.employeeId;

  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try { await fn(); toast.ok(ok); onChanged(); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); }
  };
  const review = (decision: 'approve' | 'reject') => run(decision, () => api.reviewClaim(id!, {
    decision, ...(reason.trim() ? { reason: reason.trim() } : {}),
    ...(decision === 'approve' ? { items: Object.entries(adjust).filter(([, v]) => v.amount !== '').map(([itemId, v]) => ({ id: itemId, approvedAmount: v.amount, ...(v.reason.trim() ? { reason: v.reason.trim() } : {}) })) } : {}),
  }), decision === 'approve' ? 'Claim approved' : 'Claim rejected');

  const adjustedTotal = d ? d.items.reduce((a, i) => a + Number(adjust[i.id]?.amount !== undefined && adjust[i.id]!.amount !== '' ? adjust[i.id]!.amount : i.amount), 0) : 0;

  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="lg" title={<Text fw={600}>{d?.claim.title ?? 'Expense claim'}</Text>}>
      {!d ? <Group justify="center" p="xl"><Loader size="sm" /></Group> : (
        <Stack gap="md">
          <Group justify="space-between" wrap="wrap">
            <ClaimBadge status={d.claim.status} />
            {!mine ? <Text size="sm" c="dimmed">{d.claim.employee.name} · {d.claim.employee.empCode}{d.claim.employee.grade ? ` · Grade ${d.claim.employee.grade}` : ''}</Text> : null}
          </Group>
          <SimpleGrid cols={{ base: 2, sm: 3 }}>
            <Field label="Claimed">{money(d.claim.totalClaimed)}</Field>
            <Field label="Approved">{d.claim.totalApproved ? money(d.claim.totalApproved) : '—'}</Field>
            <Field label="Submitted">{d.claim.submittedAt ? fmtAd(d.claim.submittedAt.slice(0, 10)) : 'Not yet'}</Field>
          </SimpleGrid>
          {d.payouts.length ? <Alert variant="light" color={d.payouts.some((p) => p.status === 'pending') ? 'blue' : 'teal'}>{d.payouts.map((p) => `${money(p.amount)}: ${payoutLabel(p)}`).join(' · ')}</Alert> : null}
          {d.claim.decisionNote ? <Text size="sm" c="dimmed">“{d.claim.decisionNote}”</Text> : null}
          <Issues issues={d.issues} />
          <Table.ScrollContainer minWidth={520}>
            <Table verticalSpacing="xs">
              <Table.Thead><Table.Tr><Table.Th>Date</Table.Th><Table.Th>Expense</Table.Th><Table.Th ta="right">Amount</Table.Th>{d.canDecide ? <Table.Th>Approve</Table.Th> : null}</Table.Tr></Table.Thead>
              <Table.Tbody>
                {d.items.map((i) => (
                  <Table.Tr key={i.id}>
                    <Table.Td style={{ whiteSpace: 'nowrap' }}>{fmtDateShort(i.expenseDate)}</Table.Td>
                    <Table.Td>
                      <Text size="sm">{i.description}</Text>
                      <Group gap={6}>
                        <Text size="xs" c="dimmed">{i.categoryName}{i.km ? `, ${i.km} km` : ''}</Text>
                        {i.receiptFileId ? <Anchor size="xs" href={api.receiptUrl(d.claim.id, i.id)} target="_blank" rel="noopener"><Group gap={2} wrap="nowrap"><IconPaperclip size={12} />Receipt</Group></Anchor> : null}
                      </Group>
                      {i.adjustReason ? <Text size="xs" c="orange">Approved {money(i.approvedAmount)}: {i.adjustReason}</Text> : null}
                    </Table.Td>
                    <Table.Td ta="right" style={{ whiteSpace: 'nowrap' }}>{money(i.amount)}</Table.Td>
                    {d.canDecide ? (
                      <Table.Td miw={150}>
                        <TextInput size="xs" inputMode="decimal" placeholder={i.amount} value={adjust[i.id]?.amount ?? ''} aria-label="Approved amount"
                          onChange={(e) => { const v = e.currentTarget.value.replace(/[^\d.]/g, ''); setAdjust((a) => ({ ...a, [i.id]: { amount: v, reason: a[i.id]?.reason ?? '' } })); }} />
                        {adjust[i.id]?.amount && Number(adjust[i.id]!.amount) < Number(i.amount) ? (
                          <TextInput size="xs" mt={4} placeholder="Reason for less" value={adjust[i.id]!.reason} onChange={(e) => { const v = e.currentTarget.value; setAdjust((a) => ({ ...a, [i.id]: { amount: a[i.id]!.amount, reason: v } })); }} />
                        ) : null}
                      </Table.Td>
                    ) : null}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {d.canDecide ? (
            <Paper withBorder radius="md" p="sm">
              <Stack gap="xs">
                <Text size="sm" fw={600}>Your decision</Text>
                <Text size="xs" c="dimmed">Leave the amounts empty to approve what was claimed. Approving less needs a reason on that line.</Text>
                <Textarea placeholder="Comment (required to reject)" value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} maxLength={1000} />
                <Group justify="space-between" wrap="wrap">
                  <Text size="sm">Approve <Text span fw={700}>{money(adjustedTotal)}</Text></Text>
                  <Group gap="xs">
                    <Button variant="default" color="red" onClick={() => review('reject')} loading={busy === 'reject'} disabled={!reason.trim() || !!busy}>Reject</Button>
                    <Button onClick={() => review('approve')} loading={busy === 'approve'} disabled={!!busy}>Approve</Button>
                  </Group>
                </Group>
              </Stack>
            </Paper>
          ) : null}
          {mine && (d.claim.status === 'draft' || d.claim.status === 'pending') ? (
            <Group gap="xs">
              {d.claim.status === 'draft' && onEdit ? <Button variant="default" onClick={() => onEdit(d)}>Edit</Button> : null}
              {d.claim.status === 'draft' ? <Button onClick={() => run('submit', () => api.submitClaim(d.claim.id), 'Claim submitted')} loading={busy === 'submit'} disabled={d.issues.some((i) => i.level === 'block')}>Submit for approval</Button> : null}
              <Button variant="subtle" color="red" onClick={() => run('cancel', () => api.cancelClaim(d.claim.id), 'Claim cancelled')} loading={busy === 'cancel'}>Cancel claim</Button>
            </Group>
          ) : null}
          {hasRole('hr') && d.claim.status === 'approved' && d.payouts.some((p) => p.status === 'pending' && !p.bsYear) ? (
            <>
              <Divider label="Paid another way?" labelPosition="left" />
              <Group align="flex-end" gap="xs" wrap="wrap">
                <TextInput label="How it was paid" placeholder="e.g. Cash from petty cash, voucher 112" value={cashNote} onChange={(e) => setCashNote(e.currentTarget.value)} style={{ flex: 1 }} miw={220} />
                <Button variant="default" onClick={() => run('cash', () => api.payClaimOutside(d.claim.id, cashNote.trim()), 'Marked as paid; payroll will not pay it')} loading={busy === 'cash'} disabled={cashNote.trim().length < 3}>Mark as paid</Button>
              </Group>
            </>
          ) : null}
        </Stack>
      )}
    </Drawer>
  );
}

// ---------------------------------------------------------------- travel request form (employee)
export function TravelForm({ policy, onDone }: { policy: ExpensePolicy; onDone: (id: string) => void }) {
  const [destination, setDestination] = useState('');
  const [cat, setCat] = useState<Destination>('city');
  const [purpose, setPurpose] = useState('');
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), 1));
  const [to, setTo] = useState<string | null>(addDays(todayNpt(), 2));
  const [mode, setMode] = useState<TravelMode>('bus');
  const [estimated, setEstimated] = useState('');
  const [advance, setAdvance] = useState('');
  const [fx, setFx] = useState('');
  const [quote, setQuote] = useState<TadaQuote | null>(null);
  const [quoteErr, setQuoteErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const intl = cat === 'international';

  useEffect(() => {
    if (!from || !to || to < from || (intl && !/^\d+(\.\d{1,4})?$/.test(fx))) { setQuote(null); setQuoteErr(null); return; }
    const t = setTimeout(() => {
      api.quote({ destinationCategory: cat, fromDate: from, toDate: to, ...(intl ? { fxRate: fx } : {}) })
        .then((r) => { setQuote(r.quote); setQuoteErr(null); }).catch((e: Error) => { setQuote(null); setQuoteErr(e.message); });
    }, 300);
    return () => clearTimeout(t);
  }, [cat, from, to, fx, intl]);

  const ok = destination.trim() && purpose.trim().length >= 3 && from && to && to >= from && (!estimated || isAmount(estimated)) && (!advance || isAmount(advance)) && (!intl || quote);
  const submit = async () => {
    if (!ok || !from || !to) return;
    setBusy(true);
    try {
      const r = await api.requestTravel({ destination: destination.trim(), destinationCategory: cat, purpose: purpose.trim(), fromDate: from, toDate: to, mode,
        ...(estimated ? { estimatedCost: estimated } : {}), ...(advance ? { advanceRequested: advance } : {}), ...(intl ? { fxRate: fx } : {}) });
      toast.ok('Your manager has been asked to approve the trip.', 'Travel requested');
      onDone(r.id);
    } catch (e) { toast.err(e, 'Not requested'); } finally { setBusy(false); }
  };

  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="sm">
        <TextInput label="Destination" placeholder="e.g. Pokhara" value={destination} onChange={(e) => setDestination(e.currentTarget.value)} maxLength={120} required />
        <Select label="Destination type" data={policy.destinations} value={cat} onChange={(v) => v && setCat(v as Destination)} allowDeselect={false} />
        <DateField label="From" value={from} onChange={setFrom} minDate={todayNpt()} required />
        <DateField label="To" value={to} onChange={setTo} minDate={from ?? todayNpt()} required />
        <Select label="Travel by" data={Object.entries(MODE_LABEL).map(([value, label]) => ({ value, label }))} value={mode} onChange={(v) => v && setMode(v as TravelMode)} allowDeselect={false} />
        {intl ? <TextInput label="US dollar rate (NPR)" inputMode="decimal" placeholder="e.g. 136.50" value={fx} onChange={(e) => setFx(e.currentTarget.value.replace(/[^\d.]/g, ''))} required /> : <div />}
      </SimpleGrid>
      <Textarea label="Purpose" placeholder="Why the trip is needed" value={purpose} onChange={(e) => setPurpose(e.currentTarget.value)} autosize minRows={2} maxLength={1000} required />
      {quote ? (
        <Paper withBorder radius="md" p="sm">
          <SimpleGrid cols={{ base: 2, sm: 4 }}>
            <Field label="Days">{quote.days} {quote.nights ? `(${quote.nights} ${quote.nights === 1 ? 'night' : 'nights'})` : ''}</Field>
            <Field label="Daily allowance" tip={`${quote.currency === 'USD' ? `USD ${quote.daRate} × ${quote.fxRate}` : money(quote.daRate)} per day${quote.grade !== '*' ? `, grade ${quote.grade}` : ''}`}>{money(quote.daAmount)}</Field>
            <Field label="Lodging up to" tip={`${money(quote.lodgingRateNpr)} per night`}>{money(quote.lodgingLimit)}</Field>
            <Field label="Allowance + lodging">{money(Number(quote.daAmount) + Number(quote.lodgingLimit))}</Field>
          </SimpleGrid>
        </Paper>
      ) : quoteErr ? <Text size="sm" c="red">{quoteErr}</Text> : null}
      <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="sm">
        <TextInput label="Fares and other costs (NPR)" description="Tickets, taxis; not meals or lodging" inputMode="decimal" placeholder="0" value={estimated} onChange={(e) => setEstimated(e.currentTarget.value.replace(/[^\d.]/g, ''))} />
        <TextInput label="Advance needed (NPR)" description="Paid with your salary, or in cash by HR" inputMode="decimal" placeholder="0" value={advance} onChange={(e) => setAdvance(e.currentTarget.value.replace(/[^\d.]/g, ''))} />
      </SimpleGrid>
      <ApprovalChainPreview objectType="travel_request" amount={(Number(estimated) || 0) + (quote ? Number(quote.daAmount) + Number(quote.lodgingLimit) : 0)} />
      <Group justify="flex-end"><Button onClick={submit} loading={busy} disabled={!ok}>Request approval</Button></Group>
    </Stack>
  );
}

// ---------------------------------------------------------------- trip detail, settlement and review
interface BillDraft { key: number; kind: TravelBill['kind']; billDate: string | null; description: string; amount: string; file: UploadedFile | null }
let billSeq = 1;

function SettlementForm({ d, onDone }: { d: TripDetail; onDone: () => void }) {
  const t = d.trip;
  const lastDay = t.toDate < todayNpt() ? t.toDate : todayNpt();
  const [from, setFrom] = useState<string | null>(t.fromDate);
  const [to, setTo] = useState<string | null>(lastDay);
  const [bills, setBills] = useState<BillDraft[]>([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const set = (key: number, p: Partial<BillDraft>) => setBills((bs) => bs.map((b) => (b.key === key ? { ...b, ...p } : b)));
  const fx = Number(t.fxRate ?? 1);
  const days = from && to && to >= from ? Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1 : 0;
  const da = days * Number(t.daRate) * fx;
  const lodging = bills.filter((b) => b.kind === 'lodging').reduce((a, b) => a + (Number(b.amount) || 0), 0);
  const lodgingAllowed = Math.min(lodging, Math.max(0, days - 1) * Number(t.lodgingRate) * fx);
  const other = bills.filter((b) => b.kind !== 'lodging').reduce((a, b) => a + (Number(b.amount) || 0), 0);
  const advance = t.advanceStatus !== 'none' ? Number(t.advanceApproved ?? 0) : 0;
  const net = da + lodgingAllowed + other - advance;
  const billsOk = bills.every((b) => b.billDate && b.description.trim() && isAmount(b.amount) && Number(b.amount) > 0 && (Number(b.amount) <= 500 || b.file));
  const submit = async () => {
    if (!from || !to || !billsOk) return;
    setBusy(true);
    try {
      await api.settle(t.id, { actualFrom: from, actualTo: to, bills: bills.map((b) => ({ kind: b.kind, billDate: b.billDate!, description: b.description.trim(), amount: b.amount, fileId: b.file?.id ?? null })), ...(note.trim() ? { note: note.trim() } : {}) });
      toast.ok('Sent for approval.', 'Settlement submitted');
      onDone();
    } catch (e) { toast.err(e, 'Not submitted'); } finally { setBusy(false); }
  };
  return (
    <Paper withBorder radius="md" p="sm">
      <Stack gap="sm">
        <Text fw={600} size="sm">Settle this trip</Text>
        <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="xs">
          <DateField label="Actually left" value={from} onChange={setFrom} maxDate={todayNpt()} />
          <DateField label="Actually returned" value={to} onChange={setTo} {...(from ? { minDate: from } : {})} maxDate={todayNpt()} />
        </SimpleGrid>
        <Text size="xs" c="dimmed">Meals are covered by the daily allowance. Add transport, lodging and other bills; attach the bill for anything above NPR 500.</Text>
        {bills.map((b) => (
          <Paper key={b.key} withBorder radius="sm" p="xs">
            <Group align="flex-end" gap="xs" wrap="wrap">
              <Select label="Type" data={Object.entries(BILL_KIND).map(([value, label]) => ({ value, label }))} value={b.kind} onChange={(v) => v && set(b.key, { kind: v as TravelBill['kind'] })} w={130} allowDeselect={false} />
              <DateField label="Date" value={b.billDate} onChange={(v) => set(b.key, { billDate: v })} {...(from ? { minDate: from } : {})} {...(to ? { maxDate: to } : {})} w={170} />
              <TextInput label="Description" value={b.description} onChange={(e) => set(b.key, { description: e.currentTarget.value })} style={{ flex: 1 }} miw={160} maxLength={300} />
              <TextInput label="Amount" inputMode="decimal" value={b.amount} onChange={(e) => set(b.key, { amount: e.currentTarget.value.replace(/[^\d.]/g, '') })} w={110} />
              <FileUpload purpose="travel_bill" value={b.file} onChange={(f) => set(b.key, { file: f })} label="Bill" />
              <ActionIcon variant="subtle" color="gray" aria-label="Remove bill" onClick={() => setBills((bs) => bs.filter((x) => x.key !== b.key))}><IconTrash size={16} stroke={1.6} /></ActionIcon>
            </Group>
          </Paper>
        ))}
        <Button variant="subtle" leftSection={<IconPlus size={16} />} w="fit-content" onClick={() => setBills((bs) => [...bs, { key: billSeq++, kind: 'transport', billDate: from, description: '', amount: '', file: null }])}>Add a bill</Button>
        <Textarea placeholder="Note for the approver (optional)" value={note} onChange={(e) => setNote(e.currentTarget.value)} autosize minRows={1} maxLength={1000} />
        <SimpleGrid cols={{ base: 2, sm: 4 }}>
          <Field label={`Allowance (${days} ${days === 1 ? 'day' : 'days'})`}>{money(da)}</Field>
          <Field label="Lodging allowed" {...(lodging > lodgingAllowed ? { tip: `Claimed ${money(lodging)}; capped at the nightly limit` } : {})}>{money(lodgingAllowed)}</Field>
          <Field label="Advance taken">{money(advance)}</Field>
          <Field label={net >= 0 ? 'You receive' : 'You return'}>{money(Math.abs(net))}</Field>
        </SimpleGrid>
        <Group justify="flex-end"><Button onClick={submit} loading={busy} disabled={!from || !to || to < from || !billsOk}>Submit settlement</Button></Group>
      </Stack>
    </Paper>
  );
}

function SettlementCard({ s, canDecide, onChanged }: { s: TripDetail['settlements'][number]; canDecide: boolean; onChanged: () => Promise<void> }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const net = Number(s.net);
  const decide = async (decision: 'approve' | 'reject') => {
    setBusy(decision);
    try { await api.reviewSettlement(s.id, { decision, ...(reason.trim() ? { reason: reason.trim() } : {}) }); toast.ok(decision === 'approve' ? 'Settlement approved' : 'Settlement returned'); await onChanged(); }
    catch (e) { toast.err(e); } finally { setBusy(null); }
  };
  const tone = s.status === 'approved' ? 'teal' : s.status === 'rejected' ? 'red' : 'yellow';
  return (
    <Paper withBorder radius="md" p="sm">
      <Stack gap="xs">
        <Group justify="space-between"><Text size="sm" fw={600}>Settlement, {span(s.actualFrom, s.actualTo)}</Text><Badge variant="light" color={tone}>{s.status === 'pending' ? 'Waiting for approval' : s.status === 'approved' ? 'Approved' : s.status === 'rejected' ? 'Returned' : 'Cancelled'}</Badge></Group>
        <SimpleGrid cols={{ base: 2, sm: 4 }}>
          <Field label={`Allowance (${s.days} ${s.days === 1 ? 'day' : 'days'})`}>{money(s.daAmount)}</Field>
          <Field label="Lodging allowed" {...(Number(s.lodgingClaimed) > Number(s.lodgingAllowed) ? { tip: `Claimed ${money(s.lodgingClaimed)}` } : {})}>{money(s.lodgingAllowed)}</Field>
          <Field label="Other bills">{money(s.otherBills)}</Field>
          <Field label="Advance">{money(s.advance)}</Field>
        </SimpleGrid>
        <Text size="sm">{net > 0 ? 'To pay the employee' : net < 0 ? 'To recover from the employee' : 'Nothing to pay or recover'}{net !== 0 ? <Text span fw={700}> {money(Math.abs(net))}</Text> : null}</Text>
        {s.bills.length ? (
          <Stack gap={2}>{s.bills.map((b) => (
            <Group key={b.id} gap="xs" wrap="nowrap"><Text size="xs" c="dimmed" w={90}>{fmtDateShort(b.billDate)}</Text><Text size="xs" style={{ flex: 1 }}>{BILL_KIND[b.kind]}: {b.description}</Text>
              <Text size="xs">{money(b.amount)}</Text>{b.fileId ? <Anchor size="xs" href={api.billUrl(s.id, b.id)} target="_blank" rel="noopener">Bill</Anchor> : null}</Group>
          ))}</Stack>
        ) : null}
        {s.note ? <Text size="xs" c="dimmed">“{s.note}”</Text> : null}
        {s.decisionNote ? <Text size="xs" c="dimmed">Approver: “{s.decisionNote}”</Text> : null}
        {canDecide && s.status === 'pending' ? (
          <Group align="flex-end" gap="xs" wrap="wrap">
            <TextInput placeholder="Comment (required to return)" value={reason} onChange={(e) => setReason(e.currentTarget.value)} style={{ flex: 1 }} miw={200} />
            <Button variant="default" color="red" onClick={() => decide('reject')} loading={busy === 'reject'} disabled={!reason.trim() || !!busy}>Return</Button>
            <Button onClick={() => decide('approve')} loading={busy === 'approve'} disabled={!!busy}>Approve</Button>
          </Group>
        ) : null}
      </Stack>
    </Paper>
  );
}

export function TripDrawer({ id, onClose, onChanged }: { id: string | null; onClose: () => void; onChanged: () => void }) {
  const { user, hasRole } = useAuth();
  const q = useQuery(() => (id ? api.trip(id) : Promise.resolve(null)), [id]);
  const d = q.data;
  const [reason, setReason] = useState('');
  const [advance, setAdvance] = useState('');
  const [cashNote, setCashNote] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => { setReason(''); setAdvance(''); setCashNote(''); }, [id]);
  const refresh = async () => { onChanged(); await q.reload(); };
  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try { await fn(); toast.ok(ok); await refresh(); } catch (e) { toast.err(e); } finally { setBusy(null); }
  };
  const t = d?.trip;
  const mine = t?.employee.id === user?.employeeId;

  let body: ReactNode = <Group justify="center" p="xl"><Loader size="sm" /></Group>;
  if (d && t) {
    body = (
      <Stack gap="md">
        <Group justify="space-between" wrap="wrap">
          <TripBadge status={t.status} />
          {!mine ? <Text size="sm" c="dimmed">{t.employee.name} · {t.employee.empCode}{t.employee.grade ? ` · Grade ${t.employee.grade}` : ''}</Text> : null}
        </Group>
        <SimpleGrid cols={{ base: 2, sm: 3 }}>
          <Field label="When">{span(t.fromDate, t.toDate)}</Field>
          <Field label="Where">{t.destinationLabel}</Field>
          <Field label="Travel by">{MODE_LABEL[t.mode]}</Field>
          <Field label={`Allowance (${t.days} ${t.days === 1 ? 'day' : 'days'})`} tip={t.fxRate ? `USD ${t.daRate} per day at ${t.fxRate}` : `${money(t.daRate)} per day`}>{money(t.daAmount)}</Field>
          <Field label="Lodging up to">{money(t.lodgingLimit)}</Field>
          <Field label="Fares and other costs">{money(t.estimatedCost)}</Field>
        </SimpleGrid>
        <Text size="sm">{t.purpose}</Text>
        <Paper withBorder radius="md" p="sm">
          <Group justify="space-between" wrap="wrap">
            <div>
              <Text size="xs" c="dimmed">Advance</Text>
              <Text size="sm" fw={600}>{Number(t.advanceRequested) > 0 ? `${money(t.advanceApproved ?? t.advanceRequested)}${t.advanceApproved && t.advanceApproved !== t.advanceRequested ? ` (asked ${money(t.advanceRequested)})` : ''}` : 'None requested'}</Text>
            </div>
            <Text size="sm" c="dimmed">{d.payouts.filter((p) => p.sourceType === 'travel_advance').map(payoutLabel).join(' · ') || (t.status === 'pending' && Number(t.advanceRequested) > 0 ? 'Paid once approved' : '')}</Text>
          </Group>
        </Paper>
        {t.decisionNote ? <Text size="sm" c="dimmed">Approver: “{t.decisionNote}”</Text> : null}
        {d.canDecide ? (
          <Paper withBorder radius="md" p="sm">
            <Stack gap="xs">
              <Text size="sm" fw={600}>Your decision</Text>
              {Number(t.advanceRequested) > 0 ? <TextInput label="Advance to approve (NPR)" description={`Requested ${money(t.advanceRequested)}; leave empty to approve as asked`} inputMode="decimal" value={advance} onChange={(e) => setAdvance(e.currentTarget.value.replace(/[^\d.]/g, ''))} w={260} /> : null}
              <Textarea placeholder="Comment (required to reject)" value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} maxLength={1000} />
              <Group justify="flex-end" gap="xs">
                <Button variant="default" color="red" onClick={() => run('reject', () => api.reviewTrip(t.id, { decision: 'reject', reason: reason.trim() }), 'Trip rejected')} loading={busy === 'reject'} disabled={!reason.trim() || !!busy}>Reject</Button>
                <Button onClick={() => run('approve', () => api.reviewTrip(t.id, { decision: 'approve', ...(reason.trim() ? { reason: reason.trim() } : {}), ...(advance ? { advanceApproved: advance } : {}) }), 'Trip approved')} loading={busy === 'approve'} disabled={!!busy || (!!advance && !isAmount(advance))}>Approve</Button>
              </Group>
            </Stack>
          </Paper>
        ) : null}
        {hasRole('hr') && t.advanceStatus === 'pending' && !d.payouts.some((p) => p.sourceType === 'travel_advance' && p.status === 'pending' && p.bsYear) ? (
          <Group align="flex-end" gap="xs" wrap="wrap">
            <TextInput label="Advance paid another way?" placeholder="e.g. Cash, voucher 87" value={cashNote} onChange={(e) => setCashNote(e.currentTarget.value)} style={{ flex: 1 }} miw={220} />
            <Button variant="default" onClick={() => run('cash', () => api.payAdvanceOutside(t.id, cashNote.trim()), 'Advance marked as paid')} loading={busy === 'cash'} disabled={cashNote.trim().length < 3}>Mark as paid</Button>
          </Group>
        ) : null}
        {d.canSettle ? <SettlementForm d={d} onDone={refresh} /> : null}
        {d.settlements.map((s) => <SettlementCard key={s.id} s={s} canDecide={d.canDecideSettlement && s.status === 'pending'} onChanged={refresh} />)}
        {d.payouts.some((p) => p.sourceType === 'travel_settlement') ? (
          <Text size="sm" c="dimmed">{d.payouts.filter((p) => p.sourceType === 'travel_settlement').map((p) => `${money(p.amount)}: ${payoutLabel(p)}`).join(' · ')}</Text>
        ) : null}
        {(mine || hasRole('hr')) && (t.status === 'pending' || t.status === 'approved') && t.advanceStatus !== 'paid' ? (
          <Group><Button variant="subtle" color="red" onClick={() => run('cancel', () => api.cancelTrip(t.id), 'Trip cancelled')} loading={busy === 'cancel'}>Cancel trip</Button></Group>
        ) : null}
      </Stack>
    );
  }
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="lg" title={<Text fw={600}>{t ? `Trip to ${t.destination}` : 'Trip'}</Text>}>
      {body}
    </Drawer>
  );
}
