import { Alert, Box, Button, Center, Group, Loader, Modal, PasswordInput, SimpleGrid, Stack, Text, TextInput, Title } from '@mantine/core';
import { IconCircleCheck, IconLogin2, IconLogout2, IconQrcode } from '@tabler/icons-react';
import { useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage } from '../../api/client';
import { kiosk as kioskApi, kioskKey, type KioskIdentity, type KioskSession } from '../../api/field';
import { Brand } from '../../components/Brand';
import { CameraCapture } from '../../components/CameraCapture';
import { fmtDateLong, todayNpt } from '../../lib/format';

const NPT = 345 * 60_000;
const IDLE_MS = 60_000;

function Clock({ skewMs }: { skewMs: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(id); }, []);
  const t = new Date(now + skewMs + NPT).toISOString().slice(11, 19);
  return (
    <Stack gap={0} align="center">
      <Text className="num" fz={{ base: 56, sm: 88 }} fw={600} lh={1} style={{ letterSpacing: '-0.04em' }}>{t.slice(0, 5)}<Text span fz={{ base: 24, sm: 36 }} c="dimmed">:{t.slice(6)}</Text></Text>
      <Text c="dimmed" size="lg" mt={6}>{fmtDateLong(todayNpt())}</Text>
    </Stack>
  );
}

type BarcodeDetectorLike = { detect: (src: CanvasImageSource) => Promise<Array<{ rawValue: string }>> };

/** Scans a QR badge shown on a phone with the tablet's camera (browsers with BarcodeDetector). */
function QrScanner({ opened, onClose, onCode }: { opened: boolean; onClose: () => void; onCode: (code: string) => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!opened) return;
    let stream: MediaStream | null = null; let timer = 0; let done = false;
    const Detector = (window as unknown as { BarcodeDetector?: new (o: { formats: string[] }) => BarcodeDetectorLike }).BarcodeDetector;
    if (!Detector) { setProblem('This browser cannot read QR codes. Use your code and PIN instead.'); return; }
    const detector = new Detector({ formats: ['qr_code'] });
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false });
        if (video.current) { video.current.srcObject = stream; await video.current.play().catch(() => undefined); }
        const tick = async () => {
          if (done || !video.current || video.current.readyState < 2) { timer = window.setTimeout(tick, 250); return; }
          try {
            const codes = await detector.detect(video.current);
            const hit = codes.find((c) => c.rawValue.startsWith('HQ1.'));
            if (hit) { done = true; onCode(hit.rawValue); return; }
          } catch { /* keep scanning */ }
          timer = window.setTimeout(tick, 250);
        };
        void tick();
      } catch { setProblem('The camera is not available. Use your code and PIN instead.'); }
    })();
    return () => { done = true; window.clearTimeout(timer); stream?.getTracks().forEach((t) => t.stop()); setProblem(null); };
  }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Modal opened={opened} onClose={onClose} title="Show your QR badge" centered size="lg" radius="lg">
      <Stack>
        {problem ? <Alert color="yellow" variant="light">{problem}</Alert> : (
          <Box style={{ aspectRatio: '4 / 3', borderRadius: 14, overflow: 'hidden', background: '#111' }}>
            <video ref={video} playsInline muted style={{ width: '100%', height: '100%', objectFit: 'cover', transform: 'scaleX(-1)' }} />
          </Box>
        )}
        <Text size="sm" c="dimmed">Open Hajiri on your phone, go to My attendance, Kiosk badge, and hold the code up to the camera.</Text>
      </Stack>
    </Modal>
  );
}

/**
 * Full-screen clock-in station for a shared tablet. Pair once with a code from HR; then each person enters their
 * employee code and kiosk PIN (or shows their QR badge), takes a photo, and is clocked in or out at server time.
 */
export default function KioskPage() {
  const [session, setSession] = useState<KioskSession | null>(null);
  const [state, setState] = useState<'loading' | 'unpaired' | 'ready'>('loading');
  const [skew, setSkew] = useState(0);
  const [pairCode, setPairCode] = useState('');
  const [empCode, setEmpCode] = useState(''); const [pin, setPin] = useState('');
  const [who, setWho] = useState<KioskIdentity | null>(null);
  const [camera, setCamera] = useState(false); const [scan, setScan] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ message: string; direction: 'in' | 'out' } | null>(null);
  const pinRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    if (!kioskKey.get()) { setState('unpaired'); return; }
    try {
      const s = await kioskApi.session();
      setSession(s); setSkew(Date.parse(s.serverTime) - Date.now()); setState('ready');
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { kioskKey.clear(); setState('unpaired'); }
      else { setError(errorMessage(e)); setState('ready'); }
    }
  };
  useEffect(() => { void load(); const id = window.setInterval(() => void load(), 5 * 60_000); return () => window.clearInterval(id); }, []);

  const reset = () => { setWho(null); setEmpCode(''); setPin(''); setCamera(false); setError(null); };
  // Never leave someone's name on screen: an unfinished sign-in clears itself after a minute.
  useEffect(() => { if (!who) return; const id = window.setTimeout(reset, IDLE_MS); return () => window.clearTimeout(id); }, [who]);
  useEffect(() => { if (!done) return; const id = window.setTimeout(() => setDone(null), 5000); return () => window.clearTimeout(id); }, [done]);

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 401) { kioskKey.clear(); setState('unpaired'); return; }
    setError(errorMessage(e));
  };
  const pair = async () => {
    setBusy(true); setError(null);
    try { const r = await kioskApi.pair(pairCode.trim()); kioskKey.set(r.key); setPairCode(''); await load(); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };
  const identify = async (body: { empCode: string; pin: string } | { qr: string }) => {
    setBusy(true); setError(null);
    try {
      const r = await kioskApi.identify(body);
      setWho(r); setPin('');
      if (r.selfieRequired) setCamera(true);
    } catch (e) { fail(e); setPin(''); } finally { setBusy(false); }
  };
  const punch = async (selfie?: string) => {
    if (!who) return;
    setBusy(true); setError(null);
    try {
      const r = await kioskApi.punch(who.ticket, selfie);
      setDone({ message: r.message, direction: r.direction });
      reset();
    } catch (e) {
      setCamera(false);
      if (e instanceof ApiError && (e.details as { code?: string } | undefined)?.code === 'photo_required') { setCamera(true); return; }
      fail(e); setWho(null);
    } finally { setBusy(false); }
  };

  if (state === 'loading') return <Center h="100vh"><Loader /></Center>;

  if (state === 'unpaired') {
    return (
      <Center mih="100vh" p="md">
        <Stack w="100%" maw={420} gap="lg">
          <Brand height={28} />
          <div>
            <Title order={2}>Set up this kiosk</Title>
            <Text c="dimmed" mt={4}>HR adds the kiosk under Attendance, Kiosks and gives you a one-time pairing code.</Text>
          </div>
          <TextInput size="lg" label="Pairing code" placeholder="ABCD-EFGH" value={pairCode} onChange={(e) => setPairCode(e.currentTarget.value.toUpperCase())}
            onKeyDown={(e) => { if (e.key === 'Enter' && pairCode.trim()) void pair(); }} autoFocus autoComplete="off" />
          {error ? <Alert color="red" variant="light">{error}</Alert> : null}
          <Button size="lg" loading={busy} disabled={pairCode.replace(/[^A-Z0-9]/gi, '').length < 8} onClick={pair}>Pair kiosk</Button>
        </Stack>
      </Center>
    );
  }

  const locked = session?.kiosk.lockedUntil && Date.parse(session.kiosk.lockedUntil) > Date.now();
  return (
    <Box mih="100vh" p={{ base: 'md', sm: 'xl' }} style={{ display: 'flex', flexDirection: 'column' }}>
      <Group justify="space-between">
        <Brand height={24} />
        <Text size="sm" c="dimmed">{session?.company.name}{session ? ` · ${session.kiosk.name}` : ''}</Text>
      </Group>
      <Center style={{ flex: 1 }}>
        <Stack w="100%" maw={520} gap="xl" align="stretch">
          <Clock skewMs={skew} />
          {done ? (
            <Stack align="center" gap="sm" py="lg">
              <IconCircleCheck size={64} stroke={1.4} color="var(--hj-ok)" />
              <Text fz={24} fw={600} ta="center">{done.message}</Text>
              <Button variant="subtle" onClick={() => setDone(null)}>Next person</Button>
            </Stack>
          ) : who ? (
            <Stack align="center" gap="md">
              <Text fz={28} fw={600} ta="center">Hello, {who.employee.name}</Text>
              <Text c="dimmed">{who.employee.empCode}{who.employee.designation ? ` · ${who.employee.designation}` : ''}</Text>
              {who.openBreak && who.nextDirection === 'out' ? <Alert variant="light" color="yellow" w="100%">You are on a break. Clocking out also ends it.</Alert> : null}
              <SimpleGrid cols={2} w="100%">
                <Button size="xl" variant="default" onClick={reset} disabled={busy}>Cancel</Button>
                <Button size="xl" loading={busy} leftSection={who.nextDirection === 'in' ? <IconLogin2 size={22} /> : <IconLogout2 size={22} />}
                  onClick={() => (who.selfieRequired ? setCamera(true) : void punch())}>{who.nextDirection === 'in' ? 'Clock in' : 'Clock out'}</Button>
              </SimpleGrid>
            </Stack>
          ) : (
            <Stack gap="md">
              {locked ? <Alert color="red" variant="light">This kiosk is paused after too many wrong attempts. Try again in a few minutes.</Alert> : null}
              <SimpleGrid cols={{ base: 1, xs: 2 }}>
                <TextInput size="lg" label="Employee code" placeholder="E001" value={empCode} onChange={(e) => setEmpCode(e.currentTarget.value)} autoComplete="off"
                  onKeyDown={(e) => { if (e.key === 'Enter') pinRef.current?.focus(); }} />
                <PasswordInput ref={pinRef} size="lg" label="Kiosk PIN" placeholder="4 to 6 digits" value={pin} inputMode="numeric" autoComplete="off" maxLength={6}
                  onChange={(e) => setPin(e.currentTarget.value.replace(/\D/g, ''))}
                  onKeyDown={(e) => { if (e.key === 'Enter' && empCode.trim() && pin.length >= 4) void identify({ empCode: empCode.trim(), pin }); }} />
              </SimpleGrid>
              {error ? <Alert color="red" variant="light">{error}</Alert> : null}
              <Button size="xl" loading={busy} disabled={!empCode.trim() || pin.length < 4} onClick={() => identify({ empCode: empCode.trim(), pin })}>Continue</Button>
              <Button size="lg" variant="default" leftSection={<IconQrcode size={20} />} onClick={() => { setError(null); setScan(true); }}>Scan my QR badge</Button>
            </Stack>
          )}
        </Stack>
      </Center>
      <Text size="xs" c="dimmed" ta="center">Your photo is encrypted and seen only by you, your manager and HR. Set or change your kiosk PIN in the Hajiri app under My attendance.</Text>
      <CameraCapture opened={camera && !!who} onClose={() => setCamera(false)} busy={busy} title={who ? `${who.nextDirection === 'in' ? 'Clock in' : 'Clock out'}: ${who.employee.name}` : ''}
        confirmLabel={who?.nextDirection === 'out' ? 'Clock out' : 'Clock in'} onConfirm={(p) => void punch(p)} />
      <QrScanner opened={scan} onClose={() => setScan(false)} onCode={(code) => { setScan(false); void identify({ qr: code }); }} />
    </Box>
  );
}
