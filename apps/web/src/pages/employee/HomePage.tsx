import { Anchor, Avatar, Button, Grid, Group, Modal, Paper, Stack, Text, Textarea, UnstyledButton } from '@mantine/core';
import { IconAlertTriangle, IconCamera, IconClockEdit, IconHome, IconLogin2, IconLogout2 } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { dashboard as dashApi, punch as punchApi, regularisation as regApi, shifts as shiftApi } from '../../api';
import { ApiError } from '../../api/client';
import { useAuth } from '../../auth/AuthProvider';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { liveStatus, StatusBadge } from '../../components/StatusBadge';
import { EmptyState } from '../../components/EmptyState';
import { DateField } from '../../components/BsDatePicker';
import { CameraCapture } from '../../components/CameraCapture';
import { useI18n } from '../../i18n';
import { addDays, fmtDateLong, fmtDateShort, fmtTime, greeting, minutesToHm, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { isOfflineError, queueOfflinePunch } from '../../lib/offlinePunch';
import type { Flag, Punch } from '../../api/types';

export function describeFlag(f: Flag): string {
  switch (f.kind) {
    case 'Late': return `Late by ${f.minutes} min`;
    case 'EarlyOut': return `Left ${f.minutes} min early`;
    case 'MissingOut': return 'No clock-out yet';
    case 'UnpairedPunch': return 'A clock-in or clock-out could not be paired';
    case 'OTPreapprovalMissing': return `${f.rawMinutes} min overtime without pre-approval`;
    case 'OTCapExceeded': return `Overtime cap exceeded by ${f.minutes} min`;
    case 'HolidayWork': return `Worked on a holiday (${f.workedMin} min)`;
    case 'UnpaidLeave': return 'Unpaid leave';
    case 'HalfDayLeave': return 'Half-day leave';
    case 'AutoClockOut': return 'Clocked out automatically; request a correction if this is wrong';
    case 'ClockSkew': return 'Your device clock was off, so server time was used';
    case 'OutsideLocation': return 'Clocked outside your work location (flagged for review)';
    case 'NoLocation': return 'Location was not available';
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

/** Minutes between paired clock-ins and clock-outs, counting an open clock-in up to `now`. */
function workedFromPunches(punches: Punch[], now: number): number {
  let total = 0; let openAt: number | null = null;
  const live = punches.filter((p) => !p.superseded).sort((a, b) => Date.parse(a.punched_at) - Date.parse(b.punched_at));
  for (const p of live) {
    const ms = Date.parse(p.punched_at);
    if (p.direction === 'in' && openAt === null) openAt = ms;
    else if (p.direction === 'out' && openAt !== null) { total += ms - openAt; openAt = null; }
  }
  if (openAt !== null) total += Math.max(0, now - openAt);
  return Math.floor(total / 60_000);
}

export const directionLabel = (d: string) => (d === 'in' ? 'Clocked in' : d === 'out' ? 'Clocked out' : 'Recorded');
export const sourceLabel = (s: string) => ({ web: 'Web', mobile: 'Mobile', device: 'Terminal', manual: 'Added by HR', regularisation: 'Correction' } as Record<string, string>)[s] ?? s;
const requestKind = (k: string) => ({ leave: 'Time off', ot: 'Overtime', overtime: 'Overtime', regularisation: 'Attendance correction', correction: 'Attendance correction' } as Record<string, string>)[k] ?? k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, ' ');

const CORRECTION: Record<string, string> = { wfh: 'Work from home', missed_punch: 'Missed punch', on_duty: 'On duty elsewhere', half_day: 'Half day', shift_change: 'Shift change' };

/** Server details look like "2026-10-15 wfh", "2026-10-15 to 2026-10-17" or "2026-10-15 120 min"; say them in words. */
function describeRequest(r: { kind: string; detail: string }): { title: string; detail: string } {
  const dates = (r.detail.match(/\d{4}-\d{2}-\d{2}/g) ?? []).map(fmtDateLong);
  if (r.kind === 'regularisation') {
    const k = r.detail.replace(/^\d{4}-\d{2}-\d{2}\s*/, '');
    return { title: CORRECTION[k] ?? requestKind(r.kind), detail: dates[0] ?? r.detail };
  }
  if (r.kind === 'leave') return { title: 'Time off', detail: dates.length === 2 && dates[0] !== dates[1] ? `${dates[0]} to ${dates[1]}` : dates[0] ?? r.detail };
  if (r.kind === 'ot') { const m = /(\d+)\s*min/.exec(r.detail); return { title: 'Overtime', detail: `${dates[0] ?? ''}${m ? `, ${minutesToHm(Number(m[1]))}` : ''}` }; }
  return { title: requestKind(r.kind), detail: r.detail };
}

export default function HomePage() {
  const { user } = useAuth();
  const { t } = useI18n();
  const today = useQuery(() => punchApi.today(), [], { enabled: !!user?.employeeId });
  const me = useQuery(() => dashApi.me(), [], { enabled: !!user?.employeeId });
  const shifts = useQuery(() => shiftApi.list(), [], { enabled: !!user?.employeeId });
  const [busy, setBusy] = useState(false);
  const [reasonOpen, setReasonOpen] = useState<{ direction: 'in' | 'out'; message: string } | null>(null);
  const [reason, setReason] = useState('');
  const [wfhOpen, setWfhOpen] = useState(false);
  const [wfhDate, setWfhDate] = useState<string | null>(todayNpt());
  const [wfhReason, setWfhReason] = useState('');
  const [wfhBusy, setWfhBusy] = useState(false);
  const [camera, setCamera] = useState<{ direction: 'in' | 'out'; reason?: string } | null>(null);
  const [viewPhoto, setViewPhoto] = useState<{ id: string; at: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 30_000); return () => window.clearInterval(id); }, []);

  const firstName = (user?.name ?? '').trim().split(/\s+/)[0] ?? '';
  const dateIso = today.data?.date ?? me.data?.date ?? todayNpt();

  if (!user?.employeeId) {
    return (
      <Stack gap="lg">
        <PageHeader title={`${greeting()}${firstName ? `, ${firstName}` : ''}`} subtitle={fmtDateLong(dateIso)} />
        <EmptyState title="No employee record linked" hint="Your account is not linked to an employee record, so there is nothing to clock in to. Use the menu to reach the tools for your role." />
      </Stack>
    );
  }

  const punches = today.data?.punches ?? [];
  const last = punches[punches.length - 1];
  const nextDirection: 'in' | 'out' = !last || last.direction === 'out' ? 'in' : punches.length % 2 === 0 ? 'in' : 'out';
  const day = today.data?.day ?? null;
  const wfhAllowed = !!me.data?.workFromHome?.allowed;
  const wfhToday = !!me.data?.workFromHome?.approvedToday;
  // While someone is still at work, "no clock-out yet" is expected, not a problem.
  const working = liveStatus(day?.status, { date: dateIso, today: todayNpt(), lastOut: day?.last_out, firstIn: day?.first_in }) === 'Working';
  const visibleFlags = (day?.flags ?? []).filter((f) => !(working && ['MissingOut', 'UnpairedPunch'].includes(String((f as { kind?: string }).kind))));
  const clockedIn = nextDirection === 'out';
  const firstIn = punches.find((p) => p.direction === 'in' && !p.superseded);
  const worked = clockedIn ? workedFromPunches(punches, now) : (day?.worked_min ?? workedFromPunches(punches, now));
  const shift = day?.shift_id ? shifts.data?.shifts.find((s) => s.id === day.shift_id) : undefined;

  const doPunch = async (direction: 'in' | 'out', why?: string, photo?: string) => {
    // No connection: keep the tap on this device; it goes to the manager as a correction once back online.
    const saveOffline = () => {
      if (!user) return;
      queueOfflinePunch(user.id, direction);
      setCamera(null); setReasonOpen(null);
      toast.info(`Saved on this device at ${fmtTime(new Date().toISOString())}. When you are back online it is sent to your manager as a missed ${direction === 'in' ? 'clock-in' : 'clock-out'} to approve.`, "You're offline");
    };
    if (typeof navigator !== 'undefined' && !navigator.onLine) { saveOffline(); return; }
    setBusy(true);
    // One idempotency key per attempted punch; reused only when retrying the same direction so a failed IN never swallows a later OUT.
    let key: string = crypto.randomUUID();
    try { const saved = JSON.parse(localStorage.getItem('hajiri.punchKey') ?? 'null') as { key: string; direction: string } | null; if (saved && saved.direction === direction) key = saved.key; } catch { /* ignore */ }
    localStorage.setItem('hajiri.punchKey', JSON.stringify({ key, direction }));
    try {
      const pos = await getPosition();
      const res = await punchApi.create({ direction, clientTime: new Date().toISOString(), idempotencyKey: key, source: 'web', ...(pos.lat !== undefined ? { lat: pos.lat, lng: pos.lng, accuracyM: pos.accuracyM } : {}), ...(why ? { reason: why } : {}), ...(photo ? { selfie: photo } : {}) });
      localStorage.removeItem('hajiri.punchKey');
      if (res.duplicate) toast.info(`Already recorded: ${directionLabel(res.punch.direction).toLowerCase()} at ${fmtTime(res.punch.punched_at)}.`);
      else toast.ok(res.message ?? `${directionLabel(direction)} at ${fmtTime(res.punch.punched_at)}.`);
      if (pos.denied) toast.info('Location was not available, so this was recorded without it.');
      setReasonOpen(null); setReason(''); setCamera(null);
      await Promise.all([today.reload(), me.reload()]);
    } catch (e) {
      if (isOfflineError(e)) { localStorage.removeItem('hajiri.punchKey'); saveOffline(); return; }
      // Clocking in from home needs a photo: open the camera and retry with it.
      if (e instanceof ApiError && e.status === 400 && (e.details as { code?: string } | undefined)?.code === 'photo_required') { setReasonOpen(null); setCamera({ direction, ...(why ? { reason: why } : {}) }); if (!camera) toast.info('You are clocking in from home, so a quick photo is needed.'); }
      else if (e instanceof ApiError && e.status === 400 && /reason/i.test(e.message)) { setReasonOpen({ direction, message: e.message }); }
      else toast.err(e, direction === 'in' ? 'Clock-in not saved' : 'Clock-out not saved');
    } finally { setBusy(false); }
  };

  const stateLine = clockedIn
    ? `Clocked in at ${fmtTime(last?.punched_at)}`
    : last ? `Clocked out at ${fmtTime(last.punched_at)}` : 'Not clocked in yet';
  const stateTone = clockedIn ? 'var(--hj-ok)' : last ? 'var(--hj-idle)' : 'var(--hj-warn)';

  const m = me.data?.month;
  const by = m?.byStatus ?? {};
  const presentDays = (by.Present ?? 0) + (by.WFH ?? 0) + (by.HalfDay ?? 0);
  const rowBorder = (i: number) => (i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined);

  return (
    <Stack gap="xl">
      <PageHeader title={`${greeting()}, ${firstName || 'there'}`} subtitle={fmtDateLong(dateIso)}
        actions={<>
          {!wfhAllowed ? <Button variant="default" leftSection={<IconHome size={16} stroke={1.6} />} onClick={() => { setWfhDate(todayNpt()); setWfhReason(''); setWfhOpen(true); }}>Work from home</Button> : null}
          <Button component={Link} to="/me/attendance?correct=1" variant="default" leftSection={<IconClockEdit size={16} stroke={1.6} />}>Request correction</Button>
        </>} />
      {wfhAllowed || wfhToday ? (
        <Group gap={8} wrap="nowrap" mt={-12} px={2}>
          <IconHome size={16} stroke={1.6} color="var(--hj-info)" />
          <Text size="sm" c="dimmed">{wfhAllowed ? 'You can clock in from home. Your clock-ins away from the office are marked Remote.' : 'Your work-from-home day is approved. Clock in from wherever you are.'}</Text>
        </Group>
      ) : null}

      <Grid gap="md">
        <Grid.Col span={{ base: 12, md: 7 }}>
          <Paper withBorder p="lg" h="100%">
            <Stack gap="lg" h="100%" justify="space-between">
              <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
                <Group gap={8} wrap="nowrap">
                  <span className="hj-dot" style={{ background: stateTone }} aria-hidden />
                  <Text size="sm" fw={500}>{stateLine}</Text>
                </Group>
                {day ? <StatusBadge status={liveStatus(day.status, { date: dateIso, today: todayNpt(), lastOut: day.last_out, firstIn: day.first_in })} late={day.late_min} /> : null}
              </Group>

              <div>
                <Text size="sm" c="dimmed">{clockedIn ? 'Worked so far' : 'Worked today'}</Text>
                <Text className="num" fz={40} fw={600} lh={1.1} style={{ letterSpacing: '-0.03em' }}>{minutesToHm(worked)}</Text>
                <Text size="sm" c="dimmed" mt={6}>
                  {shift ? `${shift.name_en || shift.code} shift, ${shift.start.slice(0, 5)} to ${shift.end.slice(0, 5)}` : 'No shift assigned for today'}
                  {firstIn && punches.length > 1 ? `. First clock-in ${fmtTime(firstIn.punched_at)}` : ''}
                </Text>
              </div>

              {visibleFlags.length ? (
                <Stack gap={4}>
                  {visibleFlags.map((f, i) => (
                    <Group key={i} gap={6} wrap="nowrap" align="flex-start">
                      <IconAlertTriangle size={16} stroke={1.6} color="var(--hj-warn)" style={{ flex: 'none', marginTop: 1 }} />
                      <Text size="sm" c="dimmed">{describeFlag(f)}</Text>
                    </Group>
                  ))}
                </Stack>
              ) : null}

              {nextDirection === 'in' && (wfhAllowed || wfhToday) ? (
                <Stack gap={8}>
                  <Button size="lg" fullWidth loading={busy} onClick={() => setCamera({ direction: 'in' })} leftSection={<IconCamera size={18} stroke={1.6} />}>
                    Clock in from home
                  </Button>
                  <Button variant="subtle" color="gray" size="sm" fullWidth disabled={busy} onClick={() => doPunch('in')} leftSection={<IconLogin2 size={16} stroke={1.6} />}>
                    At the office? Clock in without a photo
                  </Button>
                </Stack>
              ) : (
                <Button size="lg" fullWidth loading={busy} onClick={() => doPunch(nextDirection)}
                  leftSection={nextDirection === 'in' ? <IconLogin2 size={18} stroke={1.6} /> : <IconLogout2 size={18} stroke={1.6} />}>
                  {nextDirection === 'in' ? 'Clock in' : 'Clock out'}
                </Button>
              )}
            </Stack>
          </Paper>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 5 }}>
          <Paper withBorder p="lg" h="100%">
            <Text fw={600} mb="md">Today's activity</Text>
            {punches.length === 0 ? (
              <Text size="sm" c="dimmed">Nothing yet. Your clock-ins and clock-outs for today will show here.</Text>
            ) : (
              <Stack gap={0}>
                {punches.map((p, i) => (
                  <Group key={p.id} gap="sm" wrap="nowrap" align="stretch">
                    <Stack gap={0} align="center" w={10} style={{ flex: 'none' }}>
                      <span className="hj-dot" style={{ marginTop: 7, background: p.direction === 'in' ? 'var(--hj-ok)' : 'var(--hj-idle)' }} aria-hidden />
                      {i < punches.length - 1 ? <div style={{ width: 1, flex: 1, background: 'var(--hj-line)', marginTop: 4 }} /> : null}
                    </Stack>
                    <Group justify="space-between" wrap="nowrap" style={{ flex: 1 }} pb="sm">
                      <div>
                        <Text size="sm" fw={500} {...(p.superseded ? { td: 'line-through', c: 'dimmed' } : {})}>{directionLabel(p.direction)}</Text>
                        <Text size="xs" c="dimmed">{p.remote ? 'From home' : sourceLabel(p.source)}</Text>
                      </div>
                      <Group gap="sm" wrap="nowrap">
                        {p.photo_id ? (
                          <UnstyledButton aria-label="View clock-in photo" onClick={() => setViewPhoto({ id: p.photo_id!, at: p.punched_at })}>
                            <Avatar src={punchApi.photoUrl(p.photo_id)} size={30} radius="xl" />
                          </UnstyledButton>
                        ) : null}
                        <Text size="sm" className="num" fw={500}>{fmtTime(p.punched_at)}</Text>
                      </Group>
                    </Group>
                  </Group>
                ))}
              </Stack>
            )}
          </Paper>
        </Grid.Col>
      </Grid>

      {m ? (
        <div>
          <SectionTitle hint={`${fmtDateShort(m.from)} to ${fmtDateShort(m.to)}`} actions={<Anchor component={Link} to="/me/attendance" size="sm">View attendance</Anchor>}>This month</SectionTitle>
          <StatStrip stats={[
            { key: 'present', label: 'Present days', value: presentDays },
            { key: 'late', label: 'Late', value: minutesToHm(m.lateMin), ...(m.lateMin > 0 ? { tone: 'warn' as const } : {}) },
            { key: 'ot', label: 'Overtime', value: minutesToHm(m.otMin) },
            { key: 'absent', label: 'Absent days', value: by.Absent ?? 0, ...((by.Absent ?? 0) > 0 ? { tone: 'bad' as const } : {}) },
          ]} />
        </div>
      ) : null}

      {me.data ? (
        <Grid gap="md">
          <Grid.Col span={{ base: 12, md: 6 }}>
            <SectionTitle actions={<Anchor component={Link} to="/me/leave" size="sm">Request time off</Anchor>}>Time off balance</SectionTitle>
            {me.data.leaveBalances.length === 0 ? <EmptyState compact title="No balances yet" hint="Balances appear once HR sets up your leave policy." /> : (
              <Paper withBorder>
                {me.data.leaveBalances.map((b, i) => (
                  <Group key={b.code} justify="space-between" px="md" py={12} style={rowBorder(i)}>
                    <Text size="sm">{b.name_en}</Text>
                    <Text size="sm"><Text span fw={600} className="num">{Number(b.balance).toFixed(1)}</Text> <Text span c="dimmed" size="sm">days</Text></Text>
                  </Group>
                ))}
              </Paper>
            )}
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 6 }}>
            <SectionTitle>Pending requests</SectionTitle>
            {me.data.pendingRequests.length === 0 ? <EmptyState compact title="Nothing waiting" hint="Requests you send for approval show here until they are decided." /> : (
              <Paper withBorder>
                {me.data.pendingRequests.map((r, i) => (
                  <Group key={`${r.kind}-${r.id}`} justify="space-between" wrap="nowrap" px="md" py={12} style={rowBorder(i)}>
                    <div style={{ minWidth: 0 }}>
                      <Text size="sm" fw={500}>{describeRequest(r).title}</Text>
                      {r.detail ? <Text size="xs" c="dimmed" truncate>{describeRequest(r).detail}</Text> : null}
                    </div>
                    <Text size="xs" c="dimmed" style={{ flex: 'none' }}>{relativeTime(r.created_at)}</Text>
                  </Group>
                ))}
              </Paper>
            )}
          </Grid.Col>
        </Grid>
      ) : null}

      <Modal opened={!!reasonOpen} onClose={() => setReasonOpen(null)} title="You're outside your work location" centered>
        <Stack>
          <Text size="sm">{reasonOpen?.message}</Text>
          <Textarea label={t('reason')} description="Your manager sees this with the record." value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} required />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setReasonOpen(null)}>Cancel</Button>
            <Button disabled={!reason.trim()} loading={busy} onClick={() => reasonOpen && doPunch(reasonOpen.direction, reason)}>{reasonOpen?.direction === 'out' ? 'Clock out' : 'Clock in'}</Button>
          </Group>
        </Stack>
      </Modal>
      <CameraCapture opened={!!camera} onClose={() => setCamera(null)} busy={busy}
        title={camera?.direction === 'out' ? 'Clock out from home' : 'Clock in from home'} confirmLabel={camera?.direction === 'out' ? 'Clock out' : 'Clock in'}
        onConfirm={(photo) => { if (camera) void doPunch(camera.direction, camera.reason, photo); }} />
      <Modal opened={!!viewPhoto} onClose={() => setViewPhoto(null)} title={viewPhoto ? `Clock-in photo, ${fmtTime(viewPhoto.at)}` : ''} centered radius="lg">
        {viewPhoto ? <img src={punchApi.photoUrl(viewPhoto.id)} alt="Clock-in photo" style={{ width: '100%', borderRadius: 12, display: 'block' }} /> : null}
      </Modal>
      <Modal opened={wfhOpen} onClose={() => setWfhOpen(false)} title="Work from home" centered>
        <Stack>
          <Text size="sm" c="dimmed">Ask your manager to approve a day at home. Once approved you can clock in from home that day, and the day counts as worked.</Text>
          <DateField label="Day" value={wfhDate} onChange={setWfhDate} minDate={todayNpt()} maxDate={addDays(todayNpt(), 60)} required />
          <Textarea label="Reason" placeholder="For example: waiting for a delivery, feeling unwell but able to work" value={wfhReason} onChange={(e) => setWfhReason(e.currentTarget.value)} minRows={2} autosize required />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setWfhOpen(false)}>Cancel</Button>
            <Button loading={wfhBusy} disabled={!wfhDate || wfhReason.trim().length < 3} onClick={async () => {
              setWfhBusy(true);
              try { await regApi.create({ localDate: wfhDate, kind: 'wfh', reason: wfhReason.trim() }); toast.ok('Request sent to your manager.'); setWfhOpen(false); await me.reload(); }
              catch (e) { toast.err(e); } finally { setWfhBusy(false); }
            }}>Send request</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
