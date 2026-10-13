import { ActionIcon, Box, Group, Popover, SegmentedControl, Select, Text, TextInput, UnstyledButton } from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';
import { BS_MONTH_NAMES_EN, BS_MONTH_NAMES_NE, formatBs, supportedBsYears, toDevanagariNumerals } from '@hajiri/bs-calendar';
import { useI18n } from '../i18n';
import { bsMonthGrid, nextMonth, parseTyped, prevMonth, safe } from '../lib/bs';
import { fmtAd, todayNpt } from '../lib/format';

export interface BsDatePickerProps {
  value: string | null;                 // AD ISO date
  onChange: (ad: string | null) => void;
  label?: string;
  description?: string;
  holidays?: Map<string, string>;       // AD → name
  minDate?: string;
  maxDate?: string;
  required?: boolean;
  disabled?: boolean;
  error?: string;
  w?: number | string;
}

const DOW_EN = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
const DOW_NE = ['आ', 'सो', 'मं', 'बु', 'बि', 'शु', 'श'];

/**
 * BS-first date picker: month grid with AD in each cell, Saturdays highlighted, holidays marked, plus a typed
 * YYYY-MM-DD fallback with an explicit BS/AD switch (plan §13.5; GOV.UK date pattern).
 */
export function BsDatePicker({ value, onChange, label, description, holidays, minDate, maxDate, required, disabled, error, w }: BsDatePickerProps) {
  const { lang, numerals } = useI18n();
  const [open, setOpen] = useState(false);
  const [calendar, setCalendar] = useState<'bs' | 'ad'>('bs');
  const initial = safe(value ?? todayNpt()) ?? safe(todayNpt()) ?? { year: supportedBsYears().min, month: 1, day: 1 };
  const [view, setView] = useState({ year: initial.year, month: initial.month });
  const [typed, setTyped] = useState('');
  const [typedErr, setTypedErr] = useState<string | null>(null);

  useEffect(() => {
    if (!value) { setTyped(''); return; }
    const bs = safe(value);
    setTyped(calendar === 'bs' && bs ? formatBs(bs) : value);
    if (bs) setView({ year: bs.year, month: bs.month });
  }, [value, calendar]);

  const cells = useMemo(() => { try { return bsMonthGrid(view.year, view.month); } catch { return []; } }, [view]);
  const years = useMemo(() => { const r = supportedBsYears(); const out: string[] = []; for (let y = r.min; y <= r.max; y++) out.push(String(y)); return out; }, []);
  const months = (lang === 'ne' ? BS_MONTH_NAMES_NE : BS_MONTH_NAMES_EN).map((m, i) => ({ value: String(i + 1), label: m }));
  const num = (n: number | string) => (numerals === 'devanagari' ? toDevanagariNumerals(String(n)) : String(n));
  const today = todayNpt();
  const inBounds = (ad: string) => (!minDate || ad >= minDate) && (!maxDate || ad <= maxDate);

  const commitTyped = () => {
    if (!typed.trim()) { onChange(null); setTypedErr(null); return; }
    const ad = parseTyped(typed, calendar);
    if (!ad || !inBounds(ad)) { setTypedErr(calendar === 'bs' ? 'Enter a valid BS date as YYYY-MM-DD' : 'Enter a valid AD date as YYYY-MM-DD'); return; }
    setTypedErr(null); onChange(ad);
  };

  const bsSel = value ? safe(value) : null;
  const display = value ? (bsSel ? `${formatBs(bsSel, { numerals })} · ${fmtAd(value)}` : value) : '';

  return (
    <Box w={w}>
      <Popover opened={open} onChange={setOpen} position="bottom-start" withinPortal shadow="md" trapFocus>
        <Popover.Target>
          <TextInput
            label={label} description={description} required={required} disabled={disabled} error={error ?? typedErr ?? undefined}
            value={open ? typed : display} placeholder={calendar === 'bs' ? 'YYYY-MM-DD (BS)' : 'YYYY-MM-DD (AD)'}
            onFocus={() => setOpen(true)} onClick={() => setOpen(true)}
            onChange={(e) => setTyped(e.currentTarget.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commitTyped(); setOpen(false); } if (e.key === 'Escape') setOpen(false); }}
            onBlur={() => { if (open) commitTyped(); }}
            rightSection={<Text size="xs" c="dimmed">{calendar.toUpperCase()}</Text>}
          />
        </Popover.Target>
        <Popover.Dropdown p="sm" w={320}>
          <Group justify="space-between" mb="xs">
            <SegmentedControl size="xs" value={calendar} onChange={(v) => setCalendar(v as 'bs' | 'ad')} data={[{ value: 'bs', label: lang === 'ne' ? 'वि.सं.' : 'BS' }, { value: 'ad', label: lang === 'ne' ? 'ई.सं.' : 'AD' }]} />
            <UnstyledButton onClick={() => { onChange(today); setOpen(false); }}><Text size="xs" c="teal">{lang === 'ne' ? 'आज' : 'Today'}</Text></UnstyledButton>
          </Group>
          <Group gap={4} mb="xs" wrap="nowrap">
            <ActionIcon variant="subtle" aria-label="Previous month" onClick={() => setView(prevMonth(view.year, view.month))}>‹</ActionIcon>
            <Select size="xs" data={months} value={String(view.month)} onChange={(v) => v && setView({ ...view, month: Number(v) })} allowDeselect={false} style={{ flex: 1 }} />
            <Select size="xs" data={years} value={String(view.year)} onChange={(v) => v && setView({ ...view, year: Number(v) })} allowDeselect={false} w={90} />
            <ActionIcon variant="subtle" aria-label="Next month" onClick={() => setView(nextMonth(view.year, view.month))}>›</ActionIcon>
          </Group>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 2 }}>
            {(lang === 'ne' ? DOW_NE : DOW_EN).map((d, i) => <Text key={d} ta="center" size="xs" c={i === 6 ? 'red' : 'dimmed'} fw={600}>{d}</Text>)}
            {cells.map((c) => {
              const selected = value === c.ad;
              const hol = holidays?.get(c.ad);
              const dis = !inBounds(c.ad);
              return (
                <UnstyledButton key={c.ad} disabled={dis} title={`${c.ad}${hol ? ` · ${hol}` : ''}`} aria-label={`${formatBs(c.bs)} ${c.ad}`}
                  onClick={() => { onChange(c.ad); setOpen(false); }}
                  style={{ minHeight: 40, borderRadius: 6, padding: 2, textAlign: 'center', opacity: c.inMonth ? 1 : 0.4,
                    background: selected ? 'var(--mantine-color-teal-filled)' : c.ad === today ? 'var(--mantine-color-teal-light)' : c.saturday ? 'var(--mantine-color-red-light)' : undefined,
                    color: selected ? 'white' : undefined, outline: hol ? '1px dashed var(--mantine-color-gray-5)' : undefined, cursor: dis ? 'not-allowed' : 'pointer' }}>
                  <Text size="sm" fw={500} lh={1.1}>{num(c.bs.day)}{hol ? '★' : ''}</Text>
                  <Text size="xs" c={selected ? 'white' : 'dimmed'} lh={1}>{c.ad.slice(8)}</Text>
                </UnstyledButton>
              );
            })}
          </div>
          <Text size="xs" c="dimmed" mt="xs">{lang === 'ne' ? 'शनिबार रातो; ★ सार्वजनिक बिदा' : 'Saturdays in red; ★ public holiday'}</Text>
        </Popover.Dropdown>
      </Popover>
    </Box>
  );
}

/** Year + month selector for reports and payroll periods. */
export function BsMonthPicker({ value, onChange, label }: { value: { year: number; month: number }; onChange: (v: { year: number; month: number }) => void; label?: string }) {
  const { lang } = useI18n();
  const r = supportedBsYears();
  const years: string[] = []; for (let y = r.min; y <= r.max; y++) years.push(String(y));
  const months = (lang === 'ne' ? BS_MONTH_NAMES_NE : BS_MONTH_NAMES_EN).map((m, i) => ({ value: String(i + 1), label: m }));
  return (
    <Group gap="xs" align="flex-end">
      <Select label={label} data={months} value={String(value.month)} onChange={(v) => v && onChange({ ...value, month: Number(v) })} allowDeselect={false} w={140} />
      <Select data={years} value={String(value.year)} onChange={(v) => v && onChange({ ...value, year: Number(v) })} allowDeselect={false} w={100} />
    </Group>
  );
}
