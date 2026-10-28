import { Alert, Button, Center, Group, Paper, PasswordInput, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconLock, IconQrcode } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { field as fieldApi } from '../../api/field';
import { QrCode } from '../../components/QrCode';
import { SectionTitle } from '../../components/PageHeader';
import { fmtTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** A badge that refreshes itself every 30 seconds while it is on screen. */
function LiveBadge() {
  const [token, setToken] = useState(null);
  const [left, setLeft] = useState(30);
  const [error, setError] = useState(null);
  useEffect(() => {
    let timer = 0;
    let tick = 0;
    let alive = true;
    let until = 0;
    const load = async () => {
      try {
        const r = await fieldApi.kioskQr();
        if (!alive) return;
        setToken(r);
        setError(null);
        until = Date.now() + r.refreshInMs;
        timer = window.setTimeout(load, Math.max(1000, r.refreshInMs + 200));
      } catch {
        if (alive) {
          setError('The badge could not be loaded. Check your connection.');
          timer = window.setTimeout(load, 5000);
        }
      }
    };
    void load();
    tick = window.setInterval(() => setLeft(Math.max(0, Math.ceil((until - Date.now()) / 1000))), 500);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.clearInterval(tick);
    };
  }, []);
  if (error)
    return (
      <Alert color="yellow" variant="light">
        {error}
      </Alert>
    );
  if (!token) return null;
  return (
    <Stack align="center" gap={6}>
      <QrCode value={token.token} size={220} label="Kiosk clock-in badge" />
      <Text size="sm" fw={500}>
        {token.empCode}
      </Text>
      <Text size="xs" c="dimmed">
        New code in {left} s. Each code works once.
      </Text>
    </Stack>
  );
}

/** My attendance, Kiosk badge: the personal PIN for kiosk tablets and the rotating QR badge. */
export default function MyKioskPage() {
  const me = useQuery(() => fieldApi.me(), []);
  const [pin, setPin] = useState('');
  const [again, setAgain] = useState('');
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);
  const pinSet = me.data?.kioskPin.set;
  const lockedUntil = me.data?.kioskPin.lockedUntil;
  const save = async () => {
    setBusy(true);
    try {
      await fieldApi.setKioskPin(pin);
      toast.ok('Kiosk PIN saved.');
      setPin('');
      setAgain('');
      await me.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="lg">
      <SectionTitle hint="Clock in at a kiosk tablet with your employee code and a PIN, or by showing this QR badge to the tablet's camera.">
        Kiosk badge
      </SectionTitle>
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        <Paper withBorder p="lg">
          <Group gap={8} mb="sm">
            <IconLock size={16} stroke={1.6} />
            <Text fw={600}>Kiosk PIN</Text>
          </Group>
          {lockedUntil ? (
            <Alert color="red" variant="light" mb="sm">
              Your PIN is locked after too many wrong tries until {fmtTime(lockedUntil)}. Setting a new PIN unlocks it.
            </Alert>
          ) : null}
          <Text size="sm" c="dimmed" mb="sm">
            {pinSet ? 'Your PIN is set. Enter a new one to change it.' : 'You have not set a PIN yet.'} Use 4 to 6 digits; avoid repeated or
            consecutive digits.
          </Text>
          <Stack gap="sm">
            <PasswordInput
              label="New PIN"
              inputMode="numeric"
              maxLength={6}
              value={pin}
              onChange={(e) => setPin(e.currentTarget.value.replace(/\D/g, ''))}
              autoComplete="new-password"
            />
            <PasswordInput
              label="Repeat PIN"
              inputMode="numeric"
              maxLength={6}
              value={again}
              onChange={(e) => setAgain(e.currentTarget.value.replace(/\D/g, ''))}
              autoComplete="new-password"
              error={again && again !== pin ? 'The PINs do not match' : undefined}
            />
            <Group justify="flex-end">
              <Button loading={busy} disabled={pin.length < 4 || pin !== again} onClick={save}>
                {pinSet ? 'Change PIN' : 'Set PIN'}
              </Button>
            </Group>
          </Stack>
        </Paper>
        <Paper withBorder p="lg">
          <Group gap={8} mb="sm">
            <IconQrcode size={16} stroke={1.6} />
            <Text fw={600}>QR badge</Text>
          </Group>
          {show ? (
            <LiveBadge />
          ) : (
            <Center py="md">
              <Stack align="center" gap="xs">
                <Text size="sm" c="dimmed" ta="center" maw={320}>
                  Show the badge only at the kiosk. It changes every 30 seconds, so a photo of it is useless.
                </Text>
                <Button onClick={() => setShow(true)} leftSection={<IconQrcode size={16} />}>
                  Show my badge
                </Button>
              </Stack>
            </Center>
          )}
        </Paper>
      </SimpleGrid>
    </Stack>
  );
}
