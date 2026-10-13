import { Alert, Box, Button, Center, PasswordInput, Stack, Text, TextInput, Title } from '@mantine/core';
import { IconAlertCircle, IconClockHour4 } from '@tabler/icons-react';
import { useMemo, useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { errorMessage } from '../api/client';

/** A fixed, made-up morning used as the login illustration: arrivals cluster just before a 10:00 start. */
function useSampleMorning() {
  return useMemo(() => {
    let seed = 7;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    const marks: Array<{ m: number; late: boolean }> = [];
    for (let i = 0; i < 64; i++) {
      const g = (rnd() + rnd() + rnd()) / 3; // bell-ish
      const m = Math.round(9 * 60 + 10 + g * 75);
      marks.push({ m, late: m > 10 * 60 + 15 });
    }
    return marks.sort((a, b) => a.m - b.m);
  }, []);
}

function MorningIllustration() {
  const marks = useSampleMorning();
  const from = 8 * 60 + 30; const to = 11 * 60; const span = to - from;
  const stacks = new Map<number, number>();
  return (
    <div style={{ position: 'relative', height: 150 }}>
      <div style={{ position: 'absolute', left: `${((10 * 60 - from) / span) * 100}%`, top: 0, bottom: 22, width: `${(15 / span) * 100}%`, background: 'rgba(255,255,255,.08)', borderLeft: '1.5px solid rgba(255,255,255,.7)' }} />
      {marks.map((k, i) => {
        const b = Math.floor(k.m / 4); const level = stacks.get(b) ?? 0; stacks.set(b, level + 1);
        return <div key={i} style={{ position: 'absolute', left: `${((b * 4 - from) / span) * 100}%`, bottom: 28 + level * 10, width: 8, height: 8, borderRadius: 2, background: k.late ? '#ffb454' : 'rgba(255,255,255,.92)' }} />;
      })}
      <div style={{ position: 'absolute', left: 0, right: 0, bottom: 22, borderTop: '1px solid rgba(255,255,255,.25)' }} />
      {[9, 10, 11].map((h) => (
        <span key={h} className="num" style={{ position: 'absolute', bottom: 0, left: `${((h * 60 - from) / span) * 100}%`, transform: 'translateX(-50%)', fontSize: 12, color: 'rgba(255,255,255,.65)' }}>{`${String(h).padStart(2, '0')}:00`}</span>
      ))}
    </div>
  );
}

export default function LoginPage() {
  const { user, login, loading } = useAuth();
  const nav = useNavigate();
  const loc = useLocation() as { state?: { from?: string } };
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!loading && user) return <Navigate to={user.mustChangePassword ? '/change-password' : loc.state?.from ?? '/'} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    try { const u = await login(email.trim(), password); nav(u.mustChangePassword ? '/change-password' : loc.state?.from ?? '/', { replace: true }); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  };

  return (
    <Box style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.1fr)', minHeight: '100vh', background: 'var(--hj-surface)' }} className="hj-login">
      <style>{'@media (max-width: 62em){ .hj-login{ grid-template-columns: 1fr !important } .hj-login-art{ display:none !important } }'}</style>
      <Center p="xl">
        <Stack w="100%" maw={360} gap="xl" component="form" onSubmit={submit}>
          <Center w={36} h={36} style={{ borderRadius: 9, background: 'var(--mantine-color-brand-6)', color: 'white' }}><IconClockHour4 size={21} stroke={2} /></Center>
          <div>
            <Title order={1} fz={26} fw={600} style={{ letterSpacing: '-0.02em' }}>Sign in to Hajiri</Title>
            <Text c="dimmed" size="sm" mt={6}>Use the work email your HR team set up for you.</Text>
          </div>
          <Stack gap="md">
            {error ? <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>{error}</Alert> : null}
            <TextInput label="Email" type="email" placeholder="you@company.com" autoComplete="username" value={email} onChange={(e) => setEmail(e.currentTarget.value)} required autoFocus size="md" />
            <PasswordInput label="Password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} required size="md" />
            <Button type="submit" loading={busy} size="md" fullWidth mt={4}>Sign in</Button>
          </Stack>
          <Text size="xs" c="dimmed">Forgot your password? Ask your HR admin to reset it.</Text>
        </Stack>
      </Center>
      <Box className="hj-login-art" p={56} style={{ background: 'var(--mantine-color-brand-8)', color: 'white', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
        <Text size="sm" fw={500} c="rgba(255,255,255,.75)">Hajiri</Text>
        <div>
          <Title order={2} fz={34} fw={600} lh={1.15} maw={460} style={{ letterSpacing: '-0.025em', color: 'white' }}>Attendance, time off and payroll for the whole team.</Title>
          <Text mt="md" maw={440} c="rgba(255,255,255,.75)">Clock-ins from fingerprint and face terminals land here in seconds, and payroll reads straight from them.</Text>
        </div>
        <div>
          <MorningIllustration />
          <Text size="xs" mt="sm" c="rgba(255,255,255,.6)">A typical morning: every square is one clock-in, amber ones came in after the grace period.</Text>
        </div>
      </Box>
    </Box>
  );
}
