import { Badge, Button, Drawer, Group, NumberInput, Select, Stack, Text, Textarea, TextInput, Timeline } from '@mantine/core';
import { IconArrowsExchange, IconBriefcase, IconCircleCheck, IconDoorExit, IconNote, IconPlus, IconTrendingUp, IconUserPlus } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';
import { people, type CareerDetails, type CareerKind } from '../../api/people';
import { fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { DateField } from '../BsDatePicker';
import { EmployeePicker } from '../EmployeePicker';
import { EmptyState } from '../EmptyState';

const ICON = { size: 14, stroke: 1.8 } as const;
const KIND_ICON: Partial<Record<CareerKind, ReactNode>> = {
  hire: <IconUserPlus {...ICON} />, rehire: <IconUserPlus {...ICON} />, promotion: <IconTrendingUp {...ICON} />, confirmation: <IconCircleCheck {...ICON} />,
  transfer: <IconArrowsExchange {...ICON} />, separation: <IconDoorExit {...ICON} />, note: <IconNote {...ICON} />,
};
const KINDS: Array<{ value: CareerKind; label: string; hint: string }> = [
  { value: 'promotion', label: 'Promotion', hint: 'New designation and/or grade.' },
  { value: 'confirmation', label: 'Confirmation', hint: 'Probation completed; employment confirmed.' },
  { value: 'transfer', label: 'Transfer', hint: 'Move to another branch or department.' },
  { value: 'designation_change', label: 'Designation change', hint: 'Change of title without a promotion.' },
  { value: 'grade_change', label: 'Grade change', hint: 'Change of grade only.' },
  { value: 'manager_change', label: 'Manager change', hint: 'Report to someone else.' },
  { value: 'employment_type_change', label: 'Employment type change', hint: 'E.g. from contract to regular.' },
  { value: 'salary_revision', label: 'Salary revision note', hint: 'A record for the timeline. Update the amounts under Payroll, Employee salaries.' },
  { value: 'separation', label: 'Separation', hint: 'The person leaves on the last working day. Sign-in stops the day after.' },
  { value: 'rehire', label: 'Rejoined', hint: 'Bring back someone who had left.' },
  { value: 'note', label: 'Note', hint: 'Anything else worth keeping on the timeline.' },
];
const EMPLOYMENT = [{ value: 'regular', label: 'Regular' }, { value: 'work_based', label: 'Work-based' }, { value: 'time_based', label: 'Time-based' }, { value: 'casual', label: 'Casual' }, { value: 'part_time', label: 'Part-time' }];
const SEPARATION = [{ value: 'resignation', label: 'Resignation' }, { value: 'termination', label: 'Termination' }, { value: 'retirement', label: 'Retirement' }, { value: 'contract_end', label: 'End of contract' }, { value: 'death', label: 'Death' }, { value: 'other', label: 'Other' }];

/** The employee's career, newest first. HR records promotions, transfers, confirmations and separations from here. */
export function CareerTimeline({ employeeId, canEdit, onChanged }: { employeeId: string; canEdit: boolean; onChanged?: () => void }) {
  const q = useQuery(() => people.career(employeeId), [employeeId]);
  const structure = useQuery(() => people.structure(), [], { enabled: canEdit });
  const [form, setForm] = useState<{ kind: CareerKind; effectiveDate: string | null; details: CareerDetails; note: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const setD = (k: keyof CareerDetails, v: unknown) => setForm((f) => f && ({ ...f, details: { ...f.details, [k]: v === null || v === '' ? undefined : v } }));
  const today = todayNpt();

  const save = async () => {
    if (!form?.effectiveDate) return;
    setBusy(true);
    try {
      const details = Object.fromEntries(Object.entries(form.details).filter(([, v]) => v !== undefined)) as CareerDetails;
      const r = await people.recordCareer(employeeId, { kind: form.kind, effectiveDate: form.effectiveDate, details, ...(form.note.trim() ? { note: form.note.trim() } : {}) });
      toast.ok(r.event.status === 'scheduled' ? `Scheduled for ${fmtAd(form.effectiveDate)}. It applies automatically on that day.` : 'Recorded and applied.');
      setForm(null); await q.reload(); onChanged?.();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const cancel = async (id: string) => {
    try { await people.cancelCareer(employeeId, id); toast.ok('Scheduled change cancelled.'); await q.reload(); onChanged?.(); } catch (e) { toast.err(e); }
  };

  const k = form?.kind;
  const designations = (structure.data?.designations ?? []).filter((d) => d.active).map((d) => ({ value: d.id, label: `${d.name_en}${d.grade ? ` (${d.grade})` : ''}` }));
  return (
    <Stack gap="md">
      {canEdit ? (
        <Group justify="flex-end">
          <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setForm({ kind: 'promotion', effectiveDate: today, details: {}, note: '' })}>Record a change</Button>
        </Group>
      ) : null}
      {q.error ? <Text c="red" size="sm">{q.error}</Text> : null}
      {q.data && !q.data.events.length ? <EmptyState icon={<IconBriefcase size={20} stroke={1.6} />} title="No career events yet" hint="Promotions, transfers and confirmations appear here." /> : null}
      {q.data?.events.length ? (
        <Timeline bulletSize={26} lineWidth={2} active={q.data.events.length}>
          {q.data.events.map((ev) => (
            <Timeline.Item key={ev.id} bullet={KIND_ICON[ev.kind] ?? <IconBriefcase {...ICON} />}
              title={<Group gap="xs" wrap="wrap"><Text fw={600} size="sm">{ev.label}</Text>{ev.status === 'scheduled' ? <Badge size="sm" variant="light" color="yellow">Scheduled</Badge> : null}</Group>}>
              <Text size="xs" c="dimmed" className="num">{fmtAd(ev.effectiveDate)}{ev.createdBy ? ` · by ${ev.createdBy}` : ''}</Text>
              {ev.summary ? <Text size="sm" mt={4}>{ev.summary}</Text> : null}
              {ev.reason ? <Text size="sm" c="dimmed">Reason: {ev.reason}</Text> : null}
              {ev.note ? <Text size="sm" c="dimmed">“{ev.note}”</Text> : null}
              {canEdit && ev.status === 'scheduled' ? <Button size="compact-xs" variant="subtle" color="red" mt={4} onClick={() => cancel(ev.id)}>Cancel this change</Button> : null}
            </Timeline.Item>
          ))}
        </Timeline>
      ) : null}

      <Drawer opened={!!form} onClose={() => setForm(null)} position="right" size="md" title="Record a career change">
        {form ? (
          <Stack gap="md">
            <Select label="What happened" data={KINDS} value={form.kind} allowDeselect={false} onChange={(v) => setForm({ ...form, kind: (v ?? 'note') as CareerKind, details: {} })}
              description={KINDS.find((x) => x.value === form.kind)?.hint} />
            <DateField label={k === 'separation' ? 'Last working day' : 'Effective from'} required value={form.effectiveDate} onChange={(v) => setForm({ ...form, effectiveDate: v })}
              {...(form.effectiveDate && form.effectiveDate > today ? { description: 'A future date is scheduled and applied automatically on that day.' } : {})} />
            {k === 'promotion' || k === 'designation_change' ? (
              <Select label="New designation" searchable clearable data={designations} value={form.details.designationId ?? null} onChange={(v) => setD('designationId', v)}
                nothingFoundMessage="Add designations under Employees, Org structure" />
            ) : null}
            {k === 'promotion' || k === 'grade_change' ? <TextInput label="New grade" description={k === 'promotion' ? 'Leave blank to use the designation’s grade.' : undefined} value={form.details.grade ?? ''} onChange={(e) => setD('grade', e.currentTarget.value)} /> : null}
            {k === 'transfer' ? (
              <>
                <Select label="New branch" clearable data={(structure.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }))} value={form.details.branchId ?? null} onChange={(v) => setD('branchId', v)} />
                <Select label="New department or team" clearable searchable data={(structure.data?.units ?? []).map((u) => ({ value: u.id, label: u.name_en }))} value={form.details.orgUnitId ?? null} onChange={(v) => setD('orgUnitId', v)} />
              </>
            ) : null}
            {k === 'manager_change' || k === 'transfer' ? (
              <EmployeePicker label={k === 'transfer' ? 'New manager (optional)' : 'New manager'} value={form.details.managerId ?? null}
                onChange={(v) => setForm({ ...form, details: { ...form.details, managerId: v ?? (k === 'manager_change' ? null : undefined) } as CareerDetails })} />
            ) : null}
            {k === 'employment_type_change' ? <Select label="New employment type" data={EMPLOYMENT} value={form.details.employmentType ?? null} onChange={(v) => setD('employmentType', v)} /> : null}
            {k === 'salary_revision' ? <NumberInput label="Change (%)" description="Optional, for the record." value={form.details.revisionPercent ?? ''} onChange={(v) => setD('revisionPercent', v === '' ? undefined : Number(v))} decimalScale={2} /> : null}
            {k === 'separation' ? <Select label="Type of separation" required data={SEPARATION} value={form.details.separationType ?? null} onChange={(v) => setD('separationType', v)} /> : null}
            {k !== 'note' ? <TextInput label="Reason" value={form.details.reason ?? ''} onChange={(e) => setD('reason', e.currentTarget.value)} maxLength={500} /> : null}
            <Textarea label="Note" autosize minRows={2} maxLength={1000} value={form.note} onChange={(e) => setForm({ ...form, note: e.currentTarget.value })} />
            <Group justify="flex-end" mt="sm">
              <Button variant="default" onClick={() => setForm(null)}>Cancel</Button>
              <Button onClick={save} loading={busy} disabled={!form.effectiveDate}>Save</Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}
