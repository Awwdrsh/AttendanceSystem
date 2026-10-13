import { Alert, Badge, Button, Card, Group, Modal, Paper, SimpleGrid, Stack, Text, Textarea, Title } from '@mantine/core';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { dashboard as dashApi, punch as punchApi } from '../../api';
import { ApiError } from '../../api/client';
import { useAuth } from '../../auth/AuthProvider';
import { StatusBadge } from '../../components/StatusBadge';
import { EmptyState } from '../../components/EmptyState';
import { useI18n } from '../../i18n';
import { dualDate, fmtNum, fmtTime, minutesToHm } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import type { Flag } from '../../api/types';

function describeFlag(f: Flag, lang: 'en' | 'ne'): string {
  switch (f.kind) {
    case 'Late': return lang === 'ne' ? `ढिलो ${f.minutes} मिनेट` : `Late by ${f.minutes} min`;
    case 'EarlyOut': return lang === 'ne' ? `${f.minutes} मिनेट चाँडो बाहिर` : `Left ${f.minutes} min early`;
    case 'MissingOut': return lang === 'ne' ? 'बाहिर जाने हाजिरी छैन' : 'No OUT punch yet';
    case 'UnpairedPunch': return lang === 'ne' ? 'जोडा नमिलेको हाजिरी' : 'A punch could not be paired';
    case 'OTPreapprovalMissing': return lang === 'ne' ? `${f.rawMinutes} मिनेट ओभरटाइम, पूर्व-स्वीकृति छैन` : `${f.rawMinutes} min overtime without pre-approval`;
    case 'OTCapExceeded': return lang === 'ne' ? `ओभरटाइम सीमा नाघ्यो (${f.minutes} मिनेट)` : `Overtime cap exceeded by ${f.minutes} min`;
    case 'HolidayWork': return lang === 'ne' ? `बिदाको दिन काम (${f.workedMin} मिनेट)` : `Worked on a holiday (${f.workedMin} min)`;
    case 'UnpaidLeave': return lang === 'ne' ? 'बेतलबी बिदा' : 'Unpaid leave';
    case 'HalfDayLeave': return lang === 'ne' ? 'आधा दिन बिदा' : 'Half-day leave';
    case 'AutoClockOut': return lang === 'ne' ? 'स्वतः बाहिर गरिएको; सच्याउन अनुरोध गर्नुहोस्' : 'Clocked out automatically; request a correction if wrong';
    case 'ClockSkew': return lang === 'ne' ? 'फोनको घडी फरक थियो; सर्भरको समय प्रयोग' : 'Phone clock was off; server time used';
    case 'OutsideLocation': return lang === 'ne' ? 'कार्यस्थल बाहिरबाट हाजिरी (फ्ल्याग)' : 'Punched outside the work location (flagged)';
    case 'NoLocation': return lang === 'ne' ? 'स्थान उपलब्ध थिएन' : 'Location was not available';
    default: return f.kind;
  }
}

async function getPosition(): Promise<{ lat?: number; lng?: number; accuracyM?: number; denied: boolean }> {
  if (!('geolocation' in navigator)) return { denied: true };
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracyM: Math.round(p.coords.accuracy), denied: false }),
      () => resolve({ denied: true }), { enableHighAccuracy: true, timeout: 8000, maximumAge: 30_000 });
  });
}

export default function HomePage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const today = useQuery(() => punchApi.today(), [], { enabled: !!user?.employeeId });
  const me = useQuery(() => dashApi.me(), [], { enabled: !!user?.employeeId });
  const [busy, setBusy] = useState(false);
  const [reasonOpen, setReasonOpen] = useState<{ direction: 'in' | 'out'; message: string } | null>(null);
  const [reason, setReason] = useState('');

  if (!user?.employeeId) return <EmptyState title={t('welcome')} hint="Your account is not linked to an employee record, so there is no Today card. Use the menu for your role." />;

  const punches = today.data?.punches ?? [];
  const last = punches[punches.length - 1];
  const nextDirection: 'in' | 'out' = !last || last.direction === 'out' ? 'in' : punches.length % 2 === 0 ? 'in' : 'out';
  const day = today.data?.day ?? null;
  const d = dualDate(today.data?.date ?? me.data?.date, lang, numerals);

  const doPunch = async (direction: 'in' | 'out', why?: string) => {
    setBusy(true);
    // One idempotency key per attempted punch; reused only when retrying the same direction so a failed IN never swallows a later OUT.
    let key: string = crypto.randomUUID();
    try { const saved = JSON.parse(localStorage.getItem('hajiri.punchKey') ?? 'null') as { key: string; direction: string } | null; if (saved && saved.direction === direction) key = saved.key; } catch { /* ignore */ }
    localStorage.setItem('hajiri.punchKey', JSON.stringify({ key, direction }));
    try {
      const pos = await getPosition();
      const res = await punchApi.create({ direction, clientTime: new Date().toISOString(), idempotencyKey: key, source: 'web', ...(pos.lat !== undefined ? { lat: pos.lat, lng: pos.lng, accuracyM: pos.accuracyM } : {}), ...(why ? { reason: why } : {}) });
      localStorage.removeItem('hajiri.punchKey');
      if (res.duplicate) toast.info(`${lang === 'ne' ? 'यो हाजिरी पहिले नै दर्ता थियो' : 'This punch was already recorded'}: ${res.punch.direction.toUpperCase()} ${fmtTime(res.punch.punched_at, numerals)}`);
      else toast.ok(res.message ?? `${t('punchSaved')} ${direction.toUpperCase()} ${fmtTime(res.punch.punched_at, numerals)}`);
      if (pos.denied) toast.info(t('locationUnavailable'));
      setReasonOpen(null); setReason('');
      await Promise.all([today.reload(), me.reload()]);
    } catch (e) {
      if (e instanceof ApiError && e.status === 400 && /reason/i.test(e.message)) { setReasonOpen({ direction, message: e.message }); }
      else toast.err(e, t('punchFailed'));
    } finally { setBusy(false); }
  };

  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>{t('today')}</Title>
          <Text c="dimmed" size="sm">{d.bsLong} · {d.ad}</Text>
        </div>
      </Group>
      <Card withBorder radius="lg" p="lg">
        <Stack align="center" gap="sm">
          <Button size="xl" h={96} w="100%" maw={420} radius="xl" loading={busy} color={nextDirection === 'in' ? 'teal' : 'orange'} onClick={() => doPunch(nextDirection)} style={{ fontSize: 24 }}>
            {nextDirection === 'in' ? t('punchIn') : t('punchOut')}
          </Button>
          <Text size="sm" c="dimmed">{last ? `${lang === 'ne' ? 'अन्तिम' : 'Last'}: ${last.direction.toUpperCase()} ${fmtTime(last.punched_at, numerals)} (${last.source})` : t('noPunchesToday')}</Text>
          <Group gap="xs">
            <StatusBadge status={day?.status ?? 'Pending'} late={day?.late_min ?? null} />
            {day ? <Text size="sm">{t('worked')}: {minutesToHm(day.worked_min, numerals)}</Text> : null}
          </Group>
          {day?.flags?.length ? <Stack gap={2}>{day.flags.map((f, i) => <Text key={i} size="xs" c="dimmed">• {describeFlag(f, lang)}</Text>)}</Stack> : null}
          {punches.length ? <Group gap={6} wrap="wrap" justify="center">{punches.map((p) => <Badge key={p.id} variant="outline" color={p.direction === 'out' ? 'orange' : 'teal'}>{p.direction.toUpperCase()} {fmtTime(p.punched_at, numerals)}</Badge>)}</Group> : null}
        </Stack>
      </Card>

      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="sm">
        <Button component={Link} to="/me/leave" variant="light" h={56}>{t('leave')}</Button>
        <Button component={Link} to="/me/attendance" variant="light" h={56}>{t('myAttendance')}</Button>
        <Button component={Link} to="/me/payslips" variant="light" h={56}>{t('payslips')}</Button>
        <Button component={Link} to="/me/attendance?correct=1" variant="light" h={56}>{t('requestCorrection')}</Button>
      </SimpleGrid>

      {me.data ? (
        <SimpleGrid cols={{ base: 1, md: 2 }}>
          <Paper withBorder p="md" radius="md">
            <Text fw={600} mb="xs">{lang === 'ne' ? 'यो महिना' : 'This month'} {me.data.month.bs ? `(${fmtNum(me.data.month.bs, numerals)})` : ''}</Text>
            <Group gap="md" wrap="wrap">
              {Object.entries(me.data.month.byStatus).map(([s, n]) => <Group key={s} gap={4}><StatusBadge status={s} /><Text size="sm" fw={600}>{fmtNum(n, numerals)}</Text></Group>)}
            </Group>
            <Text size="sm" mt="xs">{t('late')}: {minutesToHm(me.data.month.lateMin, numerals)} · {t('overtime')}: {minutesToHm(me.data.month.otMin, numerals)}</Text>
          </Paper>
          <Paper withBorder p="md" radius="md">
            <Text fw={600} mb="xs">{t('balance')} · {t('leave')}</Text>
            <Group gap="md" wrap="wrap">
              {me.data.leaveBalances.map((b) => <div key={b.code}><Text size="xs" c="dimmed">{lang === 'ne' && b.name_ne ? b.name_ne : b.name_en}</Text><Text fw={700}>{fmtNum(Number(b.balance).toFixed(1), numerals)} {t('days')}</Text></div>)}
            </Group>
            {me.data.pendingRequests.length ? <Alert mt="sm" variant="light" color="violet">{me.data.pendingRequests.length} {lang === 'ne' ? 'अनुरोध विचाराधीन' : 'request(s) awaiting approval'}</Alert> : null}
          </Paper>
        </SimpleGrid>
      ) : null}

      <Modal opened={!!reasonOpen} onClose={() => setReasonOpen(null)} title={lang === 'ne' ? 'स्थान बाहिर' : 'Outside your work location'} centered>
        <Stack>
          <Text size="sm">{reasonOpen?.message}</Text>
          <Textarea label={t('reason')} value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} required />
          <Group justify="flex-end"><Button variant="default" onClick={() => setReasonOpen(null)}>{t('cancel')}</Button><Button disabled={!reason.trim()} loading={busy} onClick={() => reasonOpen && doPunch(reasonOpen.direction, reason)}>{t('submit')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
