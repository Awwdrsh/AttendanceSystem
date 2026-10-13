import { Group, Select } from '@mantine/core';
import { DateInput } from '@mantine/dates';
import { IconCalendar } from '@tabler/icons-react';
import { BS_MONTH_NAMES_EN, supportedBsYears } from '@hajiri/bs-calendar';

export interface DateFieldProps {
  value: string | null;                 // ISO date, YYYY-MM-DD
  onChange: (iso: string | null) => void;
  label?: string;
  description?: string;
  holidays?: Map<string, string>;       // ISO date → holiday name
  minDate?: string;
  maxDate?: string;
  required?: boolean;
  disabled?: boolean;
  error?: string;
  w?: number | string;
  placeholder?: string;
}

/** Calendar date input. Saturdays (Nepal's weekly off) and public holidays are tinted in the picker. */
export function DateField({ value, onChange, label, description, holidays, minDate, maxDate, required, disabled, error, w, placeholder }: DateFieldProps) {
  return (
    <DateInput
      value={value}
      onChange={(v) => onChange(v ? String(v).slice(0, 10) : null)}
      valueFormat="D MMM YYYY"
      placeholder={placeholder ?? 'Pick a date'}
      leftSection={<IconCalendar size={16} stroke={1.6} />}
      firstDayOfWeek={0}
      weekendDays={[6]}
      highlightToday
      clearable={!required}
      w={w}
      {...(label !== undefined ? { label } : {})}
      {...(description !== undefined ? { description } : {})}
      {...(minDate ? { minDate } : {})}
      {...(maxDate ? { maxDate } : {})}
      {...(required ? { required } : {})}
      {...(disabled ? { disabled } : {})}
      {...(error ? { error } : {})}
      getDayProps={(d) => {
        const name = holidays?.get(String(d).slice(0, 10));
        return name ? { title: name, style: { textDecoration: 'underline dotted', textUnderlineOffset: 3 } } : {};
      }}
    />
  );
}

/** Kept so existing pages compile; same component. */
export const BsDatePicker = DateField;

/**
 * Payroll and statutory periods follow the Nepali fiscal calendar, so months are picked by their official
 * (English-transliterated) names, e.g. "Ashwin 2083".
 */
export function BsMonthPicker({ value, onChange, label }: { value: { year: number; month: number }; onChange: (v: { year: number; month: number }) => void; label?: string }) {
  const r = supportedBsYears();
  const years: string[] = []; for (let y = r.min; y <= r.max; y++) years.push(String(y));
  const months = BS_MONTH_NAMES_EN.map((m, i) => ({ value: String(i + 1), label: m }));
  return (
    <Group gap="xs" align="flex-end">
      <Select label={label} data={months} value={String(value.month)} onChange={(v) => v && onChange({ ...value, month: Number(v) })} allowDeselect={false} w={140} />
      <Select data={years} value={String(value.year)} onChange={(v) => v && onChange({ ...value, year: Number(v) })} allowDeselect={false} w={100} />
    </Group>
  );
}
