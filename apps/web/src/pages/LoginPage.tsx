import { Alert, Anchor, Box, Button, Center, Group, PasswordInput, Stack, Text, TextInput, Title } from '@mantine/core';
import { IconAlertCircle, IconArrowRight } from '@tabler/icons-react';
import { useMemo, useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { errorMessage } from '../api/client';
import type { AuthUser } from '../api/types';
import { toast } from '../lib/toast';
import { Brand } from '../components/Brand';

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
      <div style={{ position: 'absolute', left: `${((10 * 60 - from) / span) * 100}%`, top: 0, bottom: 22, width: `${(15 / span) * 100}%`, background: 'var(--hj-hydro-soft)', borderLeft: '1.5px solid var(--hj-hydro)' }} />
      {marks.map((k, i) => {
        const b = Math.floor(k.m / 4); const level = stacks.get(b) ?? 0; stacks.set(b, level + 1);
        return <div key={i} style={{ position: 'absolute', left: `${((b * 4 - from) / span) * 100}%`, bottom: 28 + level * 10, width: 8, height: 8, borderRadius: 2, background: k.late ? 'var(--hj-warn)' : 'var(--hj-hydro)', opacity: k.late ? 0.9 : 0.55 + (level % 4) * 0.12 }} />;
      })}
      <div style={{ position: 'absolute', left: 0, right: 0, bottom: 22, borderTop: '1px solid var(--hj-line-strong)' }} />
      {[9, 10, 11].map((h) => (
        <span key={h} className="num" style={{ position: 'absolute', bottom: 0, left: `${((h * 60 - from) / span) * 100}%`, transform: 'translateX(-50%)', fontSize: 11, color: 'var(--hj-muted)', fontFamily: 'var(--hj-mono)' }}>{`${String(h).padStart(2, '0')}:00`}</span>
      ))}
    </div>
  );
}

export default function LoginPage() {
  const { user, login, completeTwoFactor, loading } = useAuth();
  const nav = useNavigate();
  const loc = useLocation() as { state?: { from?: string } };
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [useRecovery, setUseRecovery] = useState(false);

  const next = (u: AuthUser) => (u.mustChangePassword ? '/change-password' : u.mustSetUpTwoFactor ? '/setup-two-step' : loc.state?.from ?? '/');
  if (!loading && user) return <Navigate to={next(user)} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError(null);
    try {
      if (challenge) {
        const r = await completeTwoFactor(challenge, code.trim());
        if (r.usedRecoveryCode) toast.info('That recovery code is now used up. Make new ones under Profile, Sign-in security if you are running low.', 'Signed in with a recovery code');
        nav(next(r.user), { replace: true });
        return;
      }
      const r = await login(email.trim(), password);
      if ('twoFactorRequired' in r) { setChallenge(r.challenge); setCode(''); return; }
      nav(next(r), { replace: true });
    } catch (err) {
      setError(errorMessage(err));
      // An expired or used-up challenge means starting again from the password.
      if (challenge && /expired/i.test(errorMessage(err))) { setChallenge(null); setPassword(''); }
    } finally { setBusy(false); }
  };

  if (challenge) {
    return (
      <Center mih="100vh" p="md" bg="var(--hj-surface)">
        <Stack w="100%" maw={360} gap="xl" component="form" onSubmit={submit}>
          <Brand height={24} />
          <div>
            <Title order={1} fz={26} fw={700} style={{ letterSpacing: '-0.03em' }}>Two-step sign-in</Title>
            <Text c="dimmed" size="sm" mt={6}>{useRecovery ? 'Type one of the recovery codes you saved when you turned on two-step sign-in. Each code works once.' : 'Open your authenticator app and type the 6-digit code shown for Hajiri.'}</Text>
          </div>
          <Stack gap="md">
            {error ? <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>{error}</Alert> : null}
            {useRecovery
              ? <TextInput label="Recovery code" placeholder="XXXX-XXXX-XXXX" value={code} onChange={(e) => setCode(e.currentTarget.value)} required autoFocus size="md" autoComplete="off" className="mono" />
              : <TextInput label="Code" placeholder="123 456" inputMode="numeric" autoComplete="one-time-code" maxLength={7} value={code} onChange={(e) => setCode(e.currentTarget.value.replace(/[^\d ]/g, ''))} required autoFocus size="md" className="mono" />}
            <Button type="submit" loading={busy} size="md" fullWidth rightSection={<IconArrowRight size={17} />}>Verify and sign in</Button>
            <Group justify="space-between">
              <Anchor component="button" type="button" size="sm" onClick={() => { setUseRecovery(!useRecovery); setCode(''); setError(null); }}>{useRecovery ? 'Use the authenticator app' : 'Lost your phone? Use a recovery code'}</Anchor>
              <Anchor component="button" type="button" size="sm" c="dimmed" onClick={() => { setChallenge(null); setPassword(''); setError(null); }}>Back</Anchor>
            </Group>
          </Stack>
          <Text size="xs" c="dimmed">No phone and no recovery codes? Ask an administrator to reset your two-step sign-in.</Text>
        </Stack>
      </Center>
    );
  }

  return (
    <Box style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.1fr)', minHeight: '100vh', background: 'var(--hj-surface)' }} className="hj-login">
      <style>{'@media (max-width: 62em){ .hj-login{ grid-template-columns: 1fr !important } .hj-login-art{ display:none !important } }'}</style>
      <Center p="xl">
        <Stack w="100%" maw={360} gap="xl" component="form" onSubmit={submit}>
          <Brand height={24} />
          <div>
            <Title order={1} fz={28} fw={700} style={{ letterSpacing: '-0.03em' }}>Sign in</Title>
            <Text c="dimmed" size="sm" mt={6}>Use your CoreValley work email.</Text>
          </div>
          <Stack gap="md">
            {error ? <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />}>{error}</Alert> : null}
            <TextInput label="Email" type="email" placeholder="you@corevalley.ai" autoComplete="username" value={email} onChange={(e) => setEmail(e.currentTarget.value)} required autoFocus size="md" />
            <PasswordInput label="Password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} required size="md" />
            <Button type="submit" loading={busy} size="md" fullWidth mt={4} rightSection={<IconArrowRight size={17} />}>Sign in</Button>
          </Stack>
          <Text size="xs" c="dimmed">Forgot your password? Ask People Ops to reset it.</Text>
        </Stack>
      </Center>
      <Box className="hj-login-art" p={56} style={{ background: 'var(--hj-canvas)', borderLeft: '1px solid var(--hj-line)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
        backgroundImage: 'radial-gradient(rgba(21,128,61,.16) 1px, transparent 1px)', backgroundSize: '18px 18px' }}>
        <Group gap={12} wrap="nowrap">
          <span style={{ width: 40, height: 1, background: 'var(--hj-hydro)', opacity: 0.6 }} />
          <Text className="hj-eyebrow">Internal · People ops</Text>
        </Group>
        <div>
          <Title order={2} fz={46} fw={700} lh={1.05} maw={520} style={{ letterSpacing: '-0.035em' }}>
            Everything about working at CoreValley, <span style={{ color: 'var(--hj-hydro)' }}>in one place.</span>
          </Title>
          <Text mt="lg" maw={460} c="dimmed" size="md">Clock in, take time off, see your payslips. Clock-ins from the office terminals arrive in seconds, and payroll reads straight from them.</Text>
        </div>
        <div>
          <MorningIllustration />
          <Text className="mono" fz={11} mt="sm" c="dimmed" style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}>A typical morning. Each square is one clock-in; amber came in after grace.</Text>
        </div>
      </Box>
    </Box>
  );
}
