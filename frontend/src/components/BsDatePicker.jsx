import {
  ActionIcon,
  Box,
  Button,
  Group,
  InputBase,
  Popover,
  Select,
  SimpleGrid,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconCalendar, IconChevronLeft, IconChevronRight, IconX } from '@tabler/icons-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { BS_MONTH_NAMES_EN, bsToAd, formatBs, isProjectedBsYear, supportedBsYears } from '@/lib/bs-calendar';
import { bsMonthGrid, nextMonth, prevMonth, safe, shiftAd } from '../lib/bs';
import { fmtAd, fmtDateLong, fmtGregorian, todayNpt } from '../lib/format';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const AD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Sep–Oct 2026" for the Gregorian span of a BS month. */
export function adSpanLabel(firstAd, lastAd) {
  const am = AD_MONTHS[Number(firstAd.slice(5, 7)) - 1];
  const bm = AD_MONTHS[Number(lastAd.slice(5, 7)) - 1];
  return firstAd.slice(0, 4) === lastAd.slice(0, 4)
    ? `${am}–${bm} ${firstAd.slice(0, 4)}`
    : `${am} ${firstAd.slice(0, 4)}–${bm} ${lastAd.slice(0, 4)}`;
}

/**
 * Bikram Sambat date picker. The grid is a Nepali month: big BS day numbers, the Gregorian day in the corner,
 * Saturdays (the weekly off) tinted, public holidays dotted. Typing "2083-06-28" (BS) also works.
 * Values stay ISO (AD) dates because that is what the API stores.
 */
export function DateField({ value, onChange, label, description, holidays, minDate, maxDate, required, disabled, error, w, placeholder }) {
  const [opened, { open, close, toggle }] = useDisclosure(false);
  const today = todayNpt();
  const selBs = value ? safe(value) : null;
  const start = selBs ?? safe(today) ?? { year: supportedBsYears().min, month: 1, day: 1 };
  const [view, setView] = useState({ year: start.year, month: start.month });
  const [mode, setMode] = useState('days');
  const [focus, setFocus] = useState(null);
  const [typed, setTyped] = useState('');
  const [typedErr, setTypedErr] = useState(null);
  const gridRef = useRef(null);

  useEffect(() => {
    if (!opened) return;
    const b = (value ? safe(value) : null) ?? safe(today);
    if (b) setView({ year: b.year, month: b.month });
    setMode('days');
    setFocus(value ?? today);
    setTyped(value && selBs ? formatBs(selBs) : '');
    setTypedErr(null);
  }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps

  const cells = useMemo(() => {
    try {
      return bsMonthGrid(view.year, view.month);
    } catch {
      return [];
    }
  }, [view]);
  const inBounds = (ad) => (!minDate || ad >= minDate) && (!maxDate || ad <= maxDate);
  const pick = (ad) => {
    if (!inBounds(ad)) return;
    onChange(ad);
    close();
  };

  useEffect(() => {
    if (opened && mode === 'days') gridRef.current?.querySelector('[data-focus="true"]')?.focus();
  }, [focus]); // eslint-disable-line react-hooks/exhaustive-deps

  const move = (ad, days) => {
    const next = shiftAd(ad, days);
    const b = safe(next);
    if (!b) return;
    setFocus(next);
    if (b.year !== view.year || b.month !== view.month) setView({ year: b.year, month: b.month });
  };
  const onGridKey = (e) => {
    const f = focus ?? today;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (e.key in step) {
      e.preventDefault();
      move(f, step[e.key]);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      pick(f);
    } else if (e.key === 'PageUp') {
      e.preventDefault();
      setView(prevMonth(view.year, view.month));
    } else if (e.key === 'PageDown') {
      e.preventDefault();
      setView(nextMonth(view.year, view.month));
    }
  };

  /** Enter commits and closes (even if the date is unchanged); blur only commits a changed date. */
  const commitTyped = (fromEnter = false) => {
    if (!typed.trim()) {
      setTypedErr(null);
      if (fromEnter) close();
      return;
    }
    const m = /^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$/.exec(typed);
    try {
      if (!m) throw new Error('format');
      const ad = bsToAd({ year: Number(m[1]), month: Number(m[2]), day: Number(m[3]) });
      if (!inBounds(ad)) {
        setTypedErr('That date is outside the allowed range');
        return;
      }
      setTypedErr(null);
      if (ad !== value) pick(ad);
      else if (fromEnter) close();
    } catch {
      setTypedErr('Use the Nepali date as YYYY-MM-DD, for example 2083-06-28');
    }
  };

  const years = useMemo(() => {
    const r = supportedBsYears();
    const out = [];
    for (let y = r.max; y >= r.min; y--) out.push(String(y));
    return out;
  }, []);
  const inMonth = cells.filter((c) => c.inMonth);
  const adLabel = inMonth.length ? adSpanLabel(inMonth[0].ad, inMonth[inMonth.length - 1].ad) : '';

  return (
    <Box w={w}>
      <Popover
        opened={opened}
        onChange={(o) => (o ? open() : close())}
        onClose={close}
        closeOnClickOutside
        closeOnEscape
        position="bottom-start"
        shadow="md"
        withinPortal
        radius="lg"
        offset={6}
      >
        <Popover.Target>
          <InputBase
            component="button"
            type="button"
            pointer
            onClick={toggle}
            disabled={!!disabled}
            aria-haspopup="dialog"
            aria-expanded={opened}
            required={!!required}
            {...(label !== undefined ? { label } : {})}
            {...(description !== undefined ? { description } : {})}
            {...(error ? { error } : {})}
            leftSection={<IconCalendar size={16} stroke={1.6} />}
            rightSection={
              value && !required && !disabled ? (
                <ActionIcon
                  component="span"
                  size="sm"
                  variant="subtle"
                  color="gray"
                  aria-label="Clear date"
                  onClick={(e) => {
                    e.stopPropagation();
                    onChange(null);
                  }}
                >
                  <IconX size={14} />
                </ActionIcon>
              ) : null
            }
            styles={{ input: { textAlign: 'left', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }}
          >
            {value ? (
              fmtAd(value)
            ) : (
              <Text span c="dimmed" size="sm">
                {placeholder ?? 'Pick a date'}
              </Text>
            )}
          </InputBase>
        </Popover.Target>
        <Popover.Dropdown p={12} w={316} style={{ background: 'var(--hj-surface)' }}>
          <Group justify="space-between" mb={8} wrap="nowrap">
            <ActionIcon variant="subtle" color="gray" aria-label="Previous month" onClick={() => setView(prevMonth(view.year, view.month))}>
              <IconChevronLeft size={18} />
            </ActionIcon>
            <UnstyledButton
              onClick={() => setMode(mode === 'days' ? 'months' : 'days')}
              style={{ textAlign: 'center', borderRadius: 6, padding: '2px 10px' }}
              aria-label="Choose month and year"
            >
              <Text fw={700} size="sm" lh={1.25}>
                {BS_MONTH_NAMES_EN[view.month - 1]} {view.year}
              </Text>
              <Text className="mono" fz={10.5} c="dimmed" lh={1.3} style={{ letterSpacing: '0.04em' }}>
                {adLabel}
              </Text>
            </UnstyledButton>
            <ActionIcon variant="subtle" color="gray" aria-label="Next month" onClick={() => setView(nextMonth(view.year, view.month))}>
              <IconChevronRight size={18} />
            </ActionIcon>
          </Group>

          {mode === 'months' ? (
            <Box>
              <Select
                size="xs"
                mb={10}
                data={years}
                value={String(view.year)}
                onChange={(v) => v && setView({ ...view, year: Number(v) })}
                searchable
                allowDeselect={false}
                aria-label="Year (BS)"
                comboboxProps={{ withinPortal: false }}
              />
              <SimpleGrid cols={3} spacing={6}>
                {BS_MONTH_NAMES_EN.map((m, i) => (
                  <Button
                    key={m}
                    size="xs"
                    variant={i + 1 === view.month ? 'filled' : 'default'}
                    onClick={() => {
                      setView({ ...view, month: i + 1 });
                      setMode('days');
                    }}
                  >
                    {m}
                  </Button>
                ))}
              </SimpleGrid>
            </Box>
          ) : (
            <div
              ref={gridRef}
              role="grid"
              aria-label={`${BS_MONTH_NAMES_EN[view.month - 1]} ${view.year}`}
              onKeyDown={onGridKey}
              style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 2 }}
            >
              {WEEKDAYS.map((d, i) => (
                <Text
                  key={d}
                  ta="center"
                  className="mono"
                  fz={10}
                  fw={500}
                  pb={4}
                  c={i === 6 ? 'var(--hj-bad)' : 'dimmed'}
                  style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}
                >
                  {d}
                </Text>
              ))}
              {cells.map((c) => {
                const selected = value === c.ad;
                const isToday = c.ad === today;
                const hol = holidays?.get(c.ad);
                const off = !inBounds(c.ad);
                const adDay = Number(c.ad.slice(8));
                const cell = (
                  <UnstyledButton
                    key={c.ad}
                    role="gridcell"
                    disabled={off}
                    tabIndex={c.ad === (focus ?? today) ? 0 : -1}
                    data-focus={c.ad === focus}
                    aria-selected={selected}
                    aria-label={`${fmtDateLong(c.ad)} (${fmtGregorian(c.ad)})${hol ? `, ${hol}` : ''}`}
                    onClick={() => pick(c.ad)}
                    onFocus={() => setFocus(c.ad)}
                    style={{
                      position: 'relative',
                      height: 42,
                      borderRadius: 6,
                      opacity: off ? 0.3 : c.inMonth ? 1 : 0.35,
                      cursor: off ? 'not-allowed' : 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      background: selected ? 'var(--hj-hydro)' : c.saturday && c.inMonth ? 'rgba(220, 38, 38, 0.06)' : undefined,
                      color: selected ? 'var(--hj-on-hydro)' : c.saturday ? 'var(--hj-bad)' : undefined,
                      boxShadow:
                        isToday && !selected
                          ? 'inset 0 0 0 1.5px var(--hj-hydro)'
                          : c.ad === focus && !selected
                            ? 'inset 0 0 0 1px var(--hj-line-strong)'
                            : undefined,
                    }}
                  >
                    <Text component="span" fw={selected || isToday ? 800 : 600} fz={15} lh={1} className="num" mt={-6}>
                      {c.bs.day}
                    </Text>
                    <Text
                      component="span"
                      className="mono"
                      fz={9}
                      lh={1}
                      style={{ position: 'absolute', right: 4, bottom: 4, opacity: selected ? 0.85 : 0.55 }}
                    >
                      {adDay === 1 ? AD_MONTHS[Number(c.ad.slice(5, 7)) - 1] : adDay}
                    </Text>
                    {hol ? (
                      <span
                        aria-hidden
                        style={{
                          position: 'absolute',
                          left: 5,
                          bottom: 5,
                          width: 5,
                          height: 5,
                          borderRadius: 999,
                          background: selected ? 'var(--hj-on-hydro)' : 'var(--hj-warn)',
                        }}
                      />
                    ) : null}
                  </UnstyledButton>
                );
                return hol ? (
                  <Tooltip key={c.ad} label={hol} withinPortal={false}>
                    {cell}
                  </Tooltip>
                ) : (
                  cell
                );
              })}
            </div>
          )}

          <TextInput
            mt={10}
            size="xs"
            placeholder="Type a Nepali date, e.g. 2083-06-28"
            value={typed}
            error={typedErr}
            onChange={(e) => {
              setTyped(e.currentTarget.value);
              setTypedErr(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commitTyped(true);
              }
            }}
            onBlur={() => {
              if (opened) commitTyped();
            }}
            aria-label="Type a Nepali date"
            classNames={{ input: 'mono' }}
          />
          <Group justify="space-between" mt={10} wrap="nowrap" gap="xs">
            <Text size="xs" c="dimmed" lh={1.3}>
              {isProjectedBsYear(view.year)
                ? 'Projected year: may shift when the official calendar is published'
                : value
                  ? `${fmtGregorian(value)} in the Gregorian calendar`
                  : 'Saturdays in red, holidays dotted'}
            </Text>
            <Button size="compact-xs" variant="light" disabled={!inBounds(today)} onClick={() => pick(today)}>
              Today
            </Button>
          </Group>
        </Popover.Dropdown>
      </Popover>
    </Box>
  );
}

/** Kept so existing pages compile; same component. */
export const BsDatePicker = DateField;

/**
 * Payroll and statutory periods follow the Nepali fiscal calendar, so months are picked by their official
 * (English-transliterated) names, e.g. "Ashwin 2083".
 */
export function BsMonthPicker({ value, onChange, label }) {
  const r = supportedBsYears();
  const years = [];
  for (let y = r.max; y >= r.min; y--) years.push(String(y));
  const months = BS_MONTH_NAMES_EN.map((m, i) => ({ value: String(i + 1), label: m }));
  return (
    <Group gap="xs" align="flex-end">
      <Select
        label={label}
        data={months}
        value={String(value.month)}
        onChange={(v) => v && onChange({ ...value, month: Number(v) })}
        allowDeselect={false}
        w={140}
      />
      <Select
        aria-label="Year"
        data={years}
        value={String(value.year)}
        onChange={(v) => v && onChange({ ...value, year: Number(v) })}
        allowDeselect={false}
        searchable
        w={100}
      />
    </Group>
  );
}
