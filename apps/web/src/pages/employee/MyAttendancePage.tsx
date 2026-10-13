import { Badge, Button, Group, Modal, Paper, Select, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { attendance as attApi, regularisation as regApi, shifts as shiftApi } from '../../api';
import type { AttendanceDayRow, Punch } from '../../api/types';
import { bsMonthBounds } from '@hajiri/bs-calendar';
import { useAuth } from '../../auth/AuthProvider';
import { BsMonthPicker } from '../../components/BsDatePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { bsMonthGrid, safe } from '../../lib/bs';
import { dualDate, fmtNum, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

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
      toast.ok('Correction request submitted. Your manager will see it in their inbox.');
      onDone();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <Select label="What needs correcting?" value={kind} onChange={(v) => v && setKind(v as typeof kind)} allowDeselect={false} data={[
        { value: 'missed_punch', label: 'I forgot to punch' }, { value: 'wfh', label: 'I worked from home' }, { value: 'on_duty', label: 'I was on duty elsewhere' },
        { value: 'half_day', label: 'Half day' }, { value: 'shift_change', label: 'I worked a different shift' }]} />
      {kind === 'missed_punch' ? <Group grow>
        <TextInput label="Time (24 h)" value={time} onChange={(e) => setTime(e.currentTarget.value)} placeholder="HH:MM" />
        <Select label="Direction" value={direction} onChange={(v) => v && setDirection(v as 'in' | 'out')} allowDeselect={false} data={[{ value: 'in', label: 'IN' }, { value: 'out', label: 'OUT' }]} />
      </Group> : null}
      {kind === 'shift_change' ? <Select label="Shift worked" value={shiftId} onChange={setShiftId} data={(shifts.data?.shifts ?? []).map((s) => ({ value: s.id, label: `${s.code} ${s.start}–${s.end}` }))} /> : null}
      <Textarea label={t('reason')} required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} description="Required; shown to your manager." />
      <Group justify="flex-end"><Button loading={busy} disabled={reason.trim().length < 3 || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)} onClick={submit} h={48}>{t('submit')}</Button></Group>
    </Stack>
  );
}

export default function MyAttendancePage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const [params] = useSearchParams();
  const todayBs = safe(todayNpt());
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
  const holidays = new Map((cal.data?.days ?? []).filter((d) => d.holidays.length).map((d) => [d.date, d.holidays.map((h) => (lang === 'ne' && h.name_ne ? h.name_ne : h.name_en)).join(', ')]));
  const grid = useMemo(() => { try { return bsMonthGrid(ym.year, ym.month); } catch { return []; } }, [ym]);
  const sel = selected ? byDate.get(selected) : undefined;
  const selPunches = selected ? pByDate.get(selected) ?? [] : [];
  const counts: Record<string, number> = {};
  for (const d of days.data?.days ?? []) counts[d.status] = (counts[d.status] ?? 0) + 1;

  return (
    <Stack>
      <PageHeader title={t('myAttendance')} actions={<BsMonthPicker value={ym} onChange={setYm} />} />
      <Group gap="sm" wrap="wrap">{Object.entries(counts).map(([s, n]) => <Group key={s} gap={4}><StatusBadge status={s} /><Text size="sm" fw={600}>{fmtNum(n, numerals)}</Text></Group>)}</Group>
      <Paper withBorder p="xs" radius="md">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 4 }}>
          {['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].map((d, i) => <Text key={d} ta="center" size="xs" fw={600} c={i === 6 ? 'red' : 'dimmed'}>{d}</Text>)}
          {grid.map((c) => {
            const d = byDate.get(c.ad);
            const hol = holidays.get(c.ad);
            const color = d ? ({ Present: 'teal', HalfDay: 'orange', HalfDayLeave: 'blue', Absent: 'red', OnLeave: 'blue', Holiday: 'gray', WeeklyOff: 'gray', Incomplete: 'orange', WFH: 'cyan', OffShift: 'gray' } as Record<string, string>)[d.status] ?? 'gray' : undefined;
            return (
              <button key={c.ad} type="button" onClick={() => { setSelected(c.ad); setCorrect(false); }} disabled={!c.inMonth}
                style={{ minHeight: 64, borderRadius: 8, border: selected === c.ad ? '2px solid var(--mantine-color-teal-6)' : '1px solid var(--mantine-color-default-border)', background: c.ad === todayNpt() ? 'var(--mantine-color-teal-light)' : 'transparent', opacity: c.inMonth ? 1 : 0.3, cursor: c.inMonth ? 'pointer' : 'default', padding: 4, textAlign: 'left' }}>
                <Text size="sm" fw={600} {...(c.saturday ? { c: 'red' } : {})}>{fmtNum(c.bs.day, numerals)} <Text span size="xs" c="dimmed">{c.ad.slice(5)}</Text></Text>
                {d ? <Badge size="xs" variant="light" color={color ?? 'gray'}>{d.status}{d.late_min > 0 ? ' ⏱' : ''}</Badge> : hol ? <Badge size="xs" variant="light" color="gray">★</Badge> : null}
                {d?.first_in ? <Text size="xs" c="dimmed">{fmtTime(d.first_in, numerals)}{d.last_out ? `–${fmtTime(d.last_out, numerals)}` : ''}</Text> : null}
              </button>
            );
          })}
        </div>
      </Paper>

      <Modal opened={!!selected} onClose={() => setSelected(null)} title={selected ? `${dualDate(selected, lang, numerals).bsLong} · ${dualDate(selected).ad}` : ''} size="lg" centered>
        {selected ? (
          <Stack>
            <Group gap="sm"><StatusBadge status={sel?.status ?? 'Pending'} late={sel?.late_min ?? null} />{sel ? <Text size="sm">{t('worked')}: {minutesToHm(sel.worked_min, numerals)} · {t('overtime')}: {minutesToHm(sel.ot_min, numerals)}</Text> : null}</Group>
            {holidays.get(selected) ? <Text size="sm">★ {holidays.get(selected)}</Text> : null}
            {sel?.flags?.length ? <Group gap={4}>{sel.flags.map((f, i) => <Badge key={i} variant="outline" color="yellow">{f.kind}{'minutes' in f && f.minutes ? ` ${f.minutes} min` : ''}</Badge>)}</Group> : null}
            <div>
              <Text size="sm" fw={600}>{t('rawPunches')}</Text>
              {selPunches.length ? selPunches.map((p) => <Text key={p.id} size="sm" {...(p.superseded ? { c: 'dimmed', td: 'line-through' } : {})}>{fmtTime(p.punched_at, numerals)} · {p.direction.toUpperCase()} · {p.verify_mode} · {p.source}{p.device_sn ? ` · ${p.device_sn}` : ''}</Text>) : <Text size="sm" c="dimmed">{t('noPunchesToday')}</Text>}
            </div>
            {sel?.locked_at ? <Text size="xs" c="dimmed">This day is locked by payroll; ask HR for a supplementary correction.</Text> : correct ? <CorrectionForm employeeId={employeeId} date={selected} onDone={() => { setCorrect(false); setSelected(null); }} /> : <Button variant="light" onClick={() => setCorrect(true)}>{t('requestCorrection')}</Button>}
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
