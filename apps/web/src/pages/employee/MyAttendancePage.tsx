import { ActionIcon, Button, Group, Modal, Paper, Select, SimpleGrid, Stack, Text, Textarea, TextInput, Tooltip } from '@mantine/core';
import { IconAlertTriangle, IconCalendarEvent, IconChevronLeft, IconChevronRight, IconClockEdit, IconLock } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { attendance as attApi, regularisation as regApi, shifts as shiftApi } from '../../api';
import type { AttendanceDayRow, Punch } from '../../api/types';
import { bsMonthBounds } from '@hajiri/bs-calendar';
import { useAuth } from '../../auth/AuthProvider';
import { Field, StatStrip } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { liveStatus, StatusBadge, statusLabel, toneColor } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { bsMonthGrid, nextMonth, prevMonth, safe } from '../../lib/bs';
import { adSpanLabel } from '../../components/BsDatePicker';
import { bsMonthLabel, fmtDateLong, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { describeFlag, sourceLabel } from './HomePage';

export function CorrectionForm({ employeeId, date, onDone }: { employeeId: string; date: string; onDone: () => void }) {
  const { t } = useI18n();
  const [kind, setKind] = useState<'missed_punch' | 'wfh' | 'on_duty' | 'half_day' | 'shift_change'>('missed_punch');
  const [time, setTime] = useState('18:00'); const [direction, setDirection] = useState<'in' | 'out'>('out'); const [reason, setReason] = useState(''); const [shiftId, setShiftId] = useState<string | null>(null);
  const shifts = useQuery(() => shiftApi.list(), []);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const punchTime = `${date}T${time}:00+05:45`;
      await regApi.create({ employeeId, localDate: date, kind, reason, ...(kind === 'missed_punch' ? { punchTime, punchDirection: direction } : {}), ...(kind === 'shift_change' && shiftId ? { shiftId } : {}) });
      toast.ok('Correction request sent. Your manager will see it in their inbox.');
      onDone();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <Select label="What needs correcting?" value={kind} onChange={(v) => v && setKind(v as typeof kind)} allowDeselect={false} data={[
        { value: 'missed_punch', label: 'I forgot to clock in or out' }, { value: 'wfh', label: 'I worked from home' }, { value: 'on_duty', label: 'I was on duty elsewhere' },
        { value: 'half_day', label: 'I worked a half day' }, { value: 'shift_change', label: 'I worked a different shift' }]} />
      {kind === 'missed_punch' ? (
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <Select label="Missing entry" value={direction} onChange={(v) => v && setDirection(v as 'in' | 'out')} allowDeselect={false} data={[{ value: 'in', label: 'Clock-in' }, { value: 'out', label: 'Clock-out' }]} />
          <TextInput label="Time" description="24-hour, e.g. 18:00" value={time} onChange={(e) => setTime(e.currentTarget.value)} placeholder="HH:MM"
            error={/^([01]\d|2[0-3]):[0-5]\d$/.test(time) ? undefined : 'Use HH:MM, e.g. 09:30'} />
        </SimpleGrid>
      ) : null}
      {kind === 'shift_change' ? <Select label="Shift worked" value={shiftId} onChange={setShiftId} placeholder="Choose a shift" data={(shifts.data?.shifts ?? []).map((s) => ({ value: s.id, label: `${s.name_en || s.code}, ${s.start} to ${s.end}` }))} /> : null}
      <Textarea label={t('reason')} required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} description="Your manager sees this when reviewing the request." />
      <Group justify="flex-end"><Button loading={busy} disabled={reason.trim().length < 3 || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)} onClick={submit}>Send request</Button></Group>
    </Stack>
  );
}

export default function MyAttendancePage() {
  const { user } = useAuth();
  const [params] = useSearchParams();
  const todayIso = todayNpt();
  const todayBs = safe(todayIso);
  const [ym, setYm] = useState({ year: todayBs?.year ?? 2083, month: todayBs?.month ?? 1 });
  const bounds = useMemo(() => { try { return bsMonthBounds(ym.year, ym.month); } catch { return null; } }, [ym]);
  const employeeId = user?.employeeId ?? '';
  const days = useQuery(() => attApi.days(employeeId, bounds!.startAd, bounds!.endAd), [employeeId, bounds?.startAd], { enabled: !!employeeId && !!bounds });
  const punches = useQuery(() => attApi.punches(employeeId, bounds!.startAd, bounds!.endAd), [employeeId, bounds?.startAd], { enabled: !!employeeId && !!bounds });
  const cal = useQuery(() => shiftApi.calendar(bounds!.startAd, bounds!.endAd, employeeId), [employeeId, bounds?.startAd], { enabled: !!employeeId && !!bounds });
  const [selected, setSelected] = useState<string | null>(null);
  const [correct, setCorrect] = useState(false);
  useEffect(() => { if (params.get('correct')) { setSelected(todayNpt()); setCorrect(true); } }, [params]);

  const byDate = new Map<string, AttendanceDayRow>((days.data?.days ?? []).map((d) => [d.local_date, d]));
  const pByDate = new Map<string, Punch[]>();
  for (const p of punches.data?.punches ?? []) { const k = p.local_date ?? ''; pByDate.set(k, [...(pByDate.get(k) ?? []), p]); }
  const holidays = new Map((cal.data?.days ?? []).filter((d) => d.holidays.length).map((d) => [d.date, d.holidays.map((h) => h.name_en).join(', ')]));
  const grid = useMemo(() => { try { return bsMonthGrid(ym.year, ym.month); } catch { return []; } }, [ym]);
  const sel = selected ? byDate.get(selected) : undefined;
  const selPunches = selected ? pByDate.get(selected) ?? [] : [];
  const statusOf = (d: AttendanceDayRow) => liveStatus(d.status, { date: d.local_date, today: todayIso, lastOut: d.last_out, firstIn: d.first_in }) ?? d.status;

  const rows = days.data?.days ?? [];
  const count = (...s: string[]) => rows.filter((d) => s.includes(statusOf(d))).length;
  const lateDays = rows.filter((d) => d.late_min > 0).length;
  const otMin = rows.reduce((a, d) => a + (d.ot_min ?? 0), 0);
  const missing = count('Incomplete');
  const absent = count('Absent');

  return (
    <Stack gap="lg">
      <PageHeader title="My attendance"
        subtitle={bounds ? `${adSpanLabel(bounds.startAd, bounds.endAd)} in the Gregorian calendar. Select a day to see its details or request a correction.` : undefined}
        actions={
          <Group gap={6} wrap="nowrap">
            <ActionIcon variant="default" size="lg" aria-label="Previous month" onClick={() => setYm(prevMonth(ym.year, ym.month))}><IconChevronLeft size={16} stroke={1.6} /></ActionIcon>
            <Text fw={700} size="sm" w={130} ta="center">{bsMonthLabel(ym.year, ym.month)}</Text>
            <ActionIcon variant="default" size="lg" aria-label="Next month" onClick={() => setYm(nextMonth(ym.year, ym.month))}><IconChevronRight size={16} stroke={1.6} /></ActionIcon>
            {todayBs && (todayBs.year !== ym.year || todayBs.month !== ym.month) ? <Button variant="subtle" onClick={() => setYm({ year: todayBs.year, month: todayBs.month })}>This month</Button> : null}
          </Group>
        } />

      <StatStrip stats={[
        { key: 'present', label: 'Present days', value: count('Present', 'WFH', 'HalfDay', 'Working') },
        { key: 'late', label: 'Late days', value: lateDays, ...(lateDays > 0 ? { tone: 'warn' as const } : {}) },
        { key: 'leave', label: 'On leave', value: count('OnLeave', 'HalfDayLeave') },
        { key: 'absent', label: 'Absent', value: absent, ...(absent > 0 ? { tone: 'bad' as const } : {}) },
        { key: 'missing', label: 'Missing clock-out', value: missing, ...(missing > 0 ? { tone: 'warn' as const } : {}) },
        { key: 'ot', label: 'Overtime', value: minutesToHm(otMin) },
      ]} />

      <Paper withBorder p="sm" style={{ overflowX: 'auto' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(44px, 1fr))', gap: 4, minWidth: 320 }}>
          {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d, i) => <Text key={d} ta="center" className="mono" fz={10.5} c={i === 6 ? 'var(--hj-bad)' : 'dimmed'} py={4} style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}>{d}</Text>)}
          {grid.map((c) => {
            const d = byDate.get(c.ad);
            const hol = holidays.get(c.ad);
            const st = d ? statusOf(d) : null;
            const isToday = c.ad === todayIso;
            const isSel = selected === c.ad;
            const adDay = Number(c.ad.slice(8, 10));
            return (
              <button key={c.ad} type="button" onClick={() => { setSelected(c.ad); setCorrect(false); }} disabled={!c.inMonth} aria-label={fmtDateLong(c.ad)}
                style={{
                  minHeight: 72, borderRadius: 8, padding: '6px 8px', textAlign: 'left', font: 'inherit', color: 'inherit',
                  border: isSel ? '1px solid var(--hj-hydro)' : '1px solid var(--hj-line-soft)',
                  boxShadow: isSel ? '0 0 0 1px var(--hj-hydro)' : undefined,
                  background: c.saturday || hol ? 'var(--hj-canvas)' : 'var(--hj-surface)',
                  opacity: c.inMonth ? 1 : 0.35, cursor: c.inMonth ? 'pointer' : 'default',
                  display: 'flex', flexDirection: 'column', gap: 2,
                }}>
                <Group justify="space-between" wrap="nowrap" gap={4}>
                  <Text fz={16} fw={isToday ? 800 : 650} lh={1} className="num" c={isToday ? 'var(--hj-on-hydro)' : c.saturday || hol ? 'var(--hj-bad)' : 'inherit'}
                    style={isToday ? { background: 'var(--hj-hydro)', borderRadius: 6, minWidth: 26, height: 24, lineHeight: '24px', textAlign: 'center' } : undefined}>
                    {c.bs.day}
                  </Text>
                  <Text className="mono" fz={9.5} c="dimmed">{adDay === 1 || c === grid[0] ? `${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][Number(c.ad.slice(5, 7)) - 1]} ${adDay}` : adDay}</Text>
                </Group>
                {d && d.late_min > 0 ? <Tooltip label={`${d.late_min} min late`}><Text size="xs" c="var(--hj-warn)" className="num" visibleFrom="sm">{d.late_min} min late</Text></Tooltip> : null}
                {st ? (
                  <Group gap={5} wrap="nowrap" visibleFrom="sm">
                    <span className="hj-dot" style={{ background: toneColor(st) }} aria-hidden />
                    <Text size="xs" truncate>{statusLabel(st)}</Text>
                  </Group>
                ) : hol ? <Text size="xs" c="dimmed" truncate visibleFrom="sm">{hol}</Text> : null}
                {st ? <Group hiddenFrom="sm"><span className="hj-dot" style={{ background: toneColor(st) }} aria-label={statusLabel(st)} /></Group> : null}
                {d?.first_in ? <Text size="xs" c="dimmed" className="num" visibleFrom="sm">{fmtTime(d.first_in)}{d.last_out ? `–${fmtTime(d.last_out)}` : ''}</Text> : null}
              </button>
            );
          })}
        </div>
      </Paper>

      <Modal opened={!!selected} onClose={() => setSelected(null)} title={selected ? <Text fw={600}>{fmtDateLong(selected)}</Text> : ''} size="lg" centered>
        {selected ? (
          <Stack>
            <Group gap="sm"><StatusBadge status={sel ? statusOf(sel) : (holidays.get(selected) ? 'Holiday' : null)} late={sel?.late_min ?? null} /></Group>
            {holidays.get(selected) ? <Group gap={6}><IconCalendarEvent size={16} stroke={1.6} color="var(--hj-muted)" /><Text size="sm">{holidays.get(selected)}</Text></Group> : null}
            {sel ? (
              <SimpleGrid cols={{ base: 2, sm: 4 }}>
                <Field label="First clock-in">{fmtTime(sel.first_in)}</Field>
                <Field label="Last clock-out">{fmtTime(sel.last_out)}</Field>
                <Field label="Worked">{minutesToHm(sel.worked_min)}</Field>
                <Field label="Overtime">{minutesToHm(sel.ot_min)}</Field>
              </SimpleGrid>
            ) : null}
            {sel?.flags?.length ? (
              <Stack gap={4}>
                {sel.flags.map((f, i) => (
                  <Group key={i} gap={6} wrap="nowrap" align="flex-start">
                    <IconAlertTriangle size={16} stroke={1.6} color="var(--hj-warn)" style={{ flex: 'none', marginTop: 1 }} />
                    <Text size="sm" c="dimmed">{describeFlag(f)}</Text>
                  </Group>
                ))}
              </Stack>
            ) : null}
            <div>
              <Text size="sm" fw={600} mb={6}>Clock-ins and clock-outs</Text>
              {selPunches.length ? (
                <Paper withBorder>
                  {selPunches.map((p, i) => (
                    <Group key={p.id} justify="space-between" wrap="nowrap" px="sm" py={8} style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
                      <Group gap={10} wrap="nowrap">
                        <Text size="sm" fw={500} className="num" {...(p.superseded ? { c: 'dimmed', td: 'line-through' } : {})}>{fmtTime(p.punched_at)}</Text>
                        <Text size="sm" {...(p.superseded ? { c: 'dimmed', td: 'line-through' } : {})}>{p.direction === 'in' ? 'Clock-in' : p.direction === 'out' ? 'Clock-out' : 'Unpaired'}</Text>
                      </Group>
                      <Text size="xs" c="dimmed" ta="right">{[sourceLabel(p.source), p.verify_mode, p.device_sn].filter(Boolean).join(', ')}{p.superseded ? ' (replaced)' : ''}</Text>
                    </Group>
                  ))}
                </Paper>
              ) : <Text size="sm" c="dimmed">No clock-ins or clock-outs on this day.</Text>}
            </div>
            {sel?.locked_at ? (
              <Group gap={6} wrap="nowrap"><IconLock size={16} stroke={1.6} color="var(--hj-muted)" /><Text size="sm" c="dimmed">This day is locked for payroll. Ask HR for a supplementary correction.</Text></Group>
            ) : correct ? (
              <Paper withBorder p="md">
                <Text fw={600} mb="sm">Attendance correction</Text>
                <CorrectionForm employeeId={employeeId} date={selected} onDone={() => { setCorrect(false); setSelected(null); }} />
              </Paper>
            ) : (
              <Group justify="flex-end"><Button variant="default" leftSection={<IconClockEdit size={16} stroke={1.6} />} onClick={() => setCorrect(true)}>Request correction</Button></Group>
            )}
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
