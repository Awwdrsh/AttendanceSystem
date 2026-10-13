import { Alert, Button, Center, Paper, PasswordInput, SegmentedControl, Stack, Text, TextInput, Title } from '@mantine/core';
import { useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { useI18n } from '../i18n';
import { errorMessage } from '../api/client';

export default function LoginPage() {
  const { user, login, loading } = useAuth();
  const { t, lang, setLang } = useI18n();
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
    <Center mih="100vh" p="md">
      <Paper withBorder p="xl" radius="md" w="100%" maw={400} component="form" onSubmit={submit}>
        <Stack>
          <div>
            <Title order={2}>हाजिरी · Hajiri</Title>
            <Text c="dimmed" size="sm">{lang === 'ne' ? 'हाजिरी, बिदा र तलब' : 'Attendance, leave and payroll'}</Text>
          </div>
          <SegmentedControl size="xs" value={lang} onChange={(v) => setLang(v as 'en' | 'ne')} data={[{ value: 'ne', label: 'नेपाली' }, { value: 'en', label: 'English' }]} />
          {error ? <Alert color="red" variant="light">{error}</Alert> : null}
          <TextInput label={t('email')} type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.currentTarget.value)} required autoFocus />
          <PasswordInput label={t('password')} autoComplete="current-password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} required />
          <Button type="submit" loading={busy} size="md" h={48}>{t('login')}</Button>
          <Text size="xs" c="dimmed">{lang === 'ne' ? 'पासवर्ड बिर्सनुभयो? मानव संसाधनलाई सम्पर्क गर्नुहोस्।' : 'Forgot your password? Ask HR to reset it.'}</Text>
        </Stack>
      </Paper>
    </Center>
  );
}
