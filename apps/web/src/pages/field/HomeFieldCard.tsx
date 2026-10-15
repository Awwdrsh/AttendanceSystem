import { Badge, Button, Group, Modal, Paper, SimpleGrid, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { IconCamera, IconCoffee, IconMapPin, IconMapPinCheck, IconPlayerStop } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { BREAK_LABEL, currentPosition, field as fieldApi, type BreakKind } from '../../api/field';
import { ApiError } from '../../api/client';
import { CameraCapture } from '../../components/CameraCapture';
import { fmtTime, minutesToHm } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;
const since = (iso: string, now: number) => Math.max(0, Math.floor((now - Date.parse(iso)) / 60_000));

/** Home: take a break, and for field staff start or end a visit. Hidden when there is nothing to do. */
export function HomeFieldCard({ onChange }: { onChange?: () => void }) {
  const me = useQuery(() => fieldApi.me(), []);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [visitOpen, setVisitOpen] = useState(false);
  const [endOpen, setEndOpen] = useState(false);
  const [camera, setCamera] = useState(false);
  const [place, setPlace] = useState(''); const [purpose, setPurpose] = useState(''); const [outcome, setOutcome] = useState('');
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 30_000); return () => window.clearInterval(id); }, []);
  const d = me.data;
  if (!d) return null;
  const showBreaks = d.clockedIn && d.breakTypes.length > 0;
  if (!showBreaks && !d.fieldWork && d.breaks.length === 0) return null;

  const refresh = async () => { await me.reload(); onChange?.(); };
  const run = async (fn: () => Promise<void>) => { setBusy(true); try { await fn(); } catch (e) { toast.err(e); } finally { setBusy(false); } };

  const startBreak = (kind: BreakKind) => run(async () => { await fieldApi.startBreak(kind); toast.ok(`${BREAK_LABEL[kind]} break started.`); await refresh(); });
  const endBreak = () => run(async () => {
    const r = await fieldApi.endBreak();
    toast.ok(`Break ended after ${minutesToHm(r.break.minutes ?? 0)}.${r.break.over_limit ? ' That was longer than the limit, so your manager was told.' : ''}`);
    await refresh();
  });
  const startVisit = async (photo?: string) => {
    setBusy(true);
    try {
      const pos = await currentPosition();
      const r = await fieldApi.startVisit({ place: place.trim(), ...(purpose.trim() ? { purpose: purpose.trim() } : {}), ...(photo ? { photo } : {}), ...pos });
      toast.ok(r.message);
      if (pos.lat === undefined) toast.info('Location was not available, so the visit was saved without it.');
      setVisitOpen(false); setCamera(false); setPlace(''); setPurpose('');
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && (e.details as { code?: string } | undefined)?.code === 'photo_required') { setVisitOpen(false); setCamera(true); toast.info('A photo of the place is needed to start a visit.'); }
      else toast.err(e);
    } finally { setBusy(false); }
  };
  const endVisit = () => run(async () => {
    if (!d.openVisit) return;
    const pos = await currentPosition();
    await fieldApi.endVisit(d.openVisit.id, { ...(outcome.trim() ? { outcome: outcome.trim() } : {}), ...pos });
    toast.ok(`Visit to ${d.openVisit.place} ended.`);
    setEndOpen(false); setOutcome('');
    await refresh();
  });

  const breakMin = d.breaks.reduce((s, b) => s + (b.minutes ?? since(b.started_at, now)), 0);

  return (
    <SimpleGrid cols={{ base: 1, md: d.fieldWork && (showBreaks || d.breaks.length) ? 2 : 1 }} spacing="md">
      {showBreaks || d.breaks.length ? (
        <Paper withBorder p="lg">
          <Group justify="space-between" mb="sm">
            <Group gap={8}><IconCoffee {...ICON} /><Text fw={600}>Breaks</Text></Group>
            {breakMin > 0 ? <Text size="sm" c="dimmed">{minutesToHm(breakMin)} today</Text> : null}
          </Group>
          {d.openBreak ? (
            <Stack gap="sm">
              <Group gap={8} wrap="nowrap">
                <span className="hj-dot" style={{ background: since(d.openBreak.started_at, now) > d.openBreak.max_min ? 'var(--hj-bad)' : 'var(--hj-warn)' }} aria-hidden />
                <Text size="sm">On a {BREAK_LABEL[d.openBreak.kind].toLowerCase()} break since {fmtTime(d.openBreak.started_at)}, {minutesToHm(since(d.openBreak.started_at, now))} so far (limit {d.openBreak.max_min} min)</Text>
              </Group>
              <Button fullWidth loading={busy} onClick={endBreak} leftSection={<IconPlayerStop {...ICON} />}>End break</Button>
            </Stack>
          ) : showBreaks ? (
            <Group gap="xs" wrap="wrap">
              {d.breakTypes.map((t) => (
                <Button key={t.kind} variant="default" size="sm" disabled={busy} onClick={() => startBreak(t.kind)}>
                  {BREAK_LABEL[t.kind]} <Text span size="xs" c="dimmed" ml={6}>{t.maxMin} min{t.paid ? '' : ', unpaid'}</Text>
                </Button>
              ))}
            </Group>
          ) : <Text size="sm" c="dimmed">Clock in to take a break.</Text>}
          {d.breaks.filter((b) => b.ended_at).length ? (
            <Stack gap={4} mt="sm">
              {d.breaks.filter((b) => b.ended_at).map((b) => (
                <Group key={b.id} justify="space-between" wrap="nowrap">
                  <Text size="xs" c="dimmed">{BREAK_LABEL[b.kind]}, {fmtTime(b.started_at)} to {fmtTime(b.ended_at)}</Text>
                  <Text size="xs" className="num" c={b.over_limit ? 'red' : 'dimmed'}>{minutesToHm(b.minutes ?? 0)}</Text>
                </Group>
              ))}
            </Stack>
          ) : null}
        </Paper>
      ) : null}

      {d.fieldWork ? (
        <Paper withBorder p="lg">
          <Group gap={8} mb="sm"><IconMapPin {...ICON} /><Text fw={600}>Field visits</Text></Group>
          {d.openVisit ? (
            <Stack gap="sm">
              <Text size="sm">At <b>{d.openVisit.place}</b> since {fmtTime(d.openVisit.started_at)} ({minutesToHm(since(d.openVisit.started_at, now))})</Text>
              <Button fullWidth loading={busy} onClick={() => setEndOpen(true)} leftSection={<IconMapPinCheck {...ICON} />}>End visit</Button>
            </Stack>
          ) : (
            <Button fullWidth variant="default" onClick={() => setVisitOpen(true)} leftSection={<IconMapPin {...ICON} />}>Start a visit</Button>
          )}
          {d.visits.filter((v) => v.ended_at).length ? (
            <Stack gap={4} mt="sm">
              {d.visits.filter((v) => v.ended_at).map((v) => (
                <Group key={v.id} justify="space-between" wrap="nowrap">
                  <Text size="xs" c="dimmed" truncate>{v.place}</Text>
                  <Badge variant="light" color="gray" size="sm" style={{ flex: 'none' }}>{fmtTime(v.started_at)} to {fmtTime(v.ended_at)}</Badge>
                </Group>
              ))}
            </Stack>
          ) : null}
        </Paper>
      ) : null}

      <Modal opened={visitOpen} onClose={() => setVisitOpen(false)} title="Start a field visit" centered>
        <Stack>
          <TextInput label="Where are you?" placeholder="Client or place name" value={place} onChange={(e) => setPlace(e.currentTarget.value)} required maxLength={160} />
          <Textarea label="Purpose" placeholder="For example: product demo, site inspection" value={purpose} onChange={(e) => setPurpose(e.currentTarget.value)} autosize minRows={2} maxLength={500} />
          <Text size="xs" c="dimmed">Your location is recorded when the visit starts and ends.{d.visits.length === 0 && !d.clockedIn ? ' Your first visit today also clocks you in.' : ''}</Text>
          <Group justify="space-between">
            <Button variant="subtle" color="gray" disabled={!place.trim()} leftSection={<IconCamera {...ICON} />} onClick={() => { setVisitOpen(false); setCamera(true); }}>Add a photo</Button>
            <Button loading={busy} disabled={!place.trim()} onClick={() => startVisit()}>{d.fieldPhotoRequired ? 'Continue' : 'Start visit'}</Button>
          </Group>
        </Stack>
      </Modal>
      <Modal opened={endOpen} onClose={() => setEndOpen(false)} title={d.openVisit ? `End visit to ${d.openVisit.place}` : 'End visit'} centered>
        <Stack>
          <Textarea label="Outcome" placeholder="What happened? For example: order placed, follow-up next week" value={outcome} onChange={(e) => setOutcome(e.currentTarget.value)} autosize minRows={2} maxLength={1000} />
          <Group justify="flex-end"><Button variant="default" onClick={() => setEndOpen(false)}>Cancel</Button><Button loading={busy} onClick={endVisit}>End visit</Button></Group>
        </Stack>
      </Modal>
      <CameraCapture opened={camera} onClose={() => setCamera(false)} busy={busy} title={`Photo at ${place || 'the visit'}`} confirmLabel="Start visit"
        onConfirm={(photo) => { void startVisit(photo); }} />
    </SimpleGrid>
  );
}
