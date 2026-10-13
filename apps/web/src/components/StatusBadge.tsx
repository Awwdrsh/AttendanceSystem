import { Badge } from '@mantine/core';
import { useI18n } from '../i18n';
import type { AttendanceStatus } from '../api/types';

/** Icon + text + colour, never colour alone (plan §13.1, §13.12). */
const MAP: Record<string, { icon: string; color: string; en: string; ne: string }> = {
  Present: { icon: '✓', color: 'teal', en: 'Present', ne: 'उपस्थित' },
  Late: { icon: '⏱', color: 'yellow', en: 'Late', ne: 'ढिलो' },
  Absent: { icon: '✕', color: 'red', en: 'Absent', ne: 'अनुपस्थित' },
  OnLeave: { icon: '◐', color: 'blue', en: 'On leave', ne: 'बिदामा' },
  HalfDayLeave: { icon: '◐', color: 'blue', en: 'Half-day leave', ne: 'आधा दिन बिदा' },
  HalfDay: { icon: '◑', color: 'orange', en: 'Half day', ne: 'आधा दिन' },
  Holiday: { icon: '★', color: 'gray', en: 'Holiday', ne: 'सार्वजनिक बिदा' },
  WeeklyOff: { icon: '☾', color: 'gray', en: 'Weekly off', ne: 'साप्ताहिक बिदा' },
  Incomplete: { icon: '!', color: 'orange', en: 'Incomplete', ne: 'अपूर्ण' },
  WFH: { icon: '⌂', color: 'cyan', en: 'Work from home', ne: 'घरबाट काम' },
  OffShift: { icon: '–', color: 'gray', en: 'Off shift', ne: 'सिफ्ट बाहिर' },
  Pending: { icon: '⋯', color: 'violet', en: 'Pending', ne: 'विचाराधीन' },
  pending: { icon: '⋯', color: 'violet', en: 'Pending', ne: 'विचाराधीन' },
  approved: { icon: '✓', color: 'teal', en: 'Approved', ne: 'स्वीकृत' },
  rejected: { icon: '✕', color: 'red', en: 'Rejected', ne: 'अस्वीकृत' },
  cancelled: { icon: '–', color: 'gray', en: 'Cancelled', ne: 'रद्द' },
  detected: { icon: '⋯', color: 'violet', en: 'Detected', ne: 'पत्ता लागेको' },
  posted: { icon: '✓', color: 'teal', en: 'Posted', ne: 'पोस्ट भयो' },
  comp_off: { icon: '◐', color: 'blue', en: 'Comp-off', ne: 'सट्टा बिदा' },
  active: { icon: '✓', color: 'teal', en: 'Active', ne: 'सक्रिय' },
  disabled: { icon: '✕', color: 'red', en: 'Disabled', ne: 'निष्क्रिय' },
  exited: { icon: '–', color: 'gray', en: 'Exited', ne: 'बाहिरिएको' },
  draft: { icon: '⋯', color: 'gray', en: 'Draft', ne: 'मस्यौदा' },
  preflight_failed: { icon: '!', color: 'red', en: 'Blocked', ne: 'अवरुद्ध' },
  computed: { icon: '∑', color: 'violet', en: 'Calculated', ne: 'गणना भयो' },
  reviewed: { icon: '◉', color: 'blue', en: 'Reviewed', ne: 'समीक्षा भयो' },
  locked: { icon: '🔒', color: 'teal', en: 'Locked', ne: 'बन्द' },
  disbursed: { icon: '₨', color: 'teal', en: 'Paid', ne: 'भुक्तानी भयो' },
  filed: { icon: '✓', color: 'teal', en: 'Filed', ne: 'दाखिला भयो' },
  submitted: { icon: '⋯', color: 'violet', en: 'Submitted', ne: 'पेश भयो' },
  verified: { icon: '✓', color: 'teal', en: 'Verified', ne: 'प्रमाणित' },
  NotComputed: { icon: '?', color: 'gray', en: 'Not computed', ne: 'गणना भएको छैन' },
};

export function StatusBadge({ status, late }: { status: AttendanceStatus | string | null | undefined; late?: number | null }) {
  const { lang } = useI18n();
  if (!status) return <Badge variant="light" color="gray">—</Badge>;
  const m = MAP[status] ?? { icon: '·', color: 'gray', en: status, ne: status };
  const label = lang === 'ne' ? m.ne : m.en;
  return (
    <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
      <Badge variant="light" color={m.color} leftSection={<span aria-hidden>{m.icon}</span>}>{label}</Badge>
      {late && late > 0 ? <Badge variant="light" color="yellow" leftSection={<span aria-hidden>⏱</span>}>{lang === 'ne' ? `ढिलो ${late} मि.` : `Late ${late} min`}</Badge> : null}
    </span>
  );
}

export function statusLabel(status: string, lang: 'en' | 'ne'): string {
  const m = MAP[status];
  return m ? (lang === 'ne' ? m.ne : m.en) : status;
}
