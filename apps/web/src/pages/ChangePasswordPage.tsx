import { Alert, Button, Center, Paper, PasswordInput, Stack, Text, Title } from '@mantine/core';
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { auth as authApi } from '../api';
import { errorMessage } from '../api/client';
import { useAuth } from '../auth/AuthProvider';
import { useI18n } from '../i18n';
import { toast } from '../lib/toast';

export default function ChangePasswordPage() {
  const { t } = useI18n();
  const { user, refresh } = useAuth();
  const nav = useNavigate();
  const [cur, setCur] = useState(''); const [next, setNext] = useState(''); const [again, setAgain] = useState('');
  const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== again) { setError('The two new passwords do not match.'); return; }
    if (next.length < 12) { setError('Use at least 12 characters. A short sentence works well.'); return; }
    setBusy(true); setError(null);
    try { await authApi.changePassword(cur, next); toast.ok('Password changed. Please sign in again.'); await refresh(); nav('/login', { replace: true }); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  };
  return (
    <Center mih="80vh" p="md">
      <Paper withBorder p="xl" radius="md" w="100%" maw={420} component="form" onSubmit={submit}>
        <Stack>
          <Title order={3}>{t('changePassword')}</Title>
          {user?.mustChangePassword ? <Alert color="yellow" variant="light">{t('mustChange')}</Alert> : null}
          {error ? <Alert color="red" variant="light">{error}</Alert> : null}
          <PasswordInput label={t('currentPassword')} value={cur} onChange={(e) => setCur(e.currentTarget.value)} required autoComplete="current-password" />
          <PasswordInput label={t('newPassword')} description="At least 12 characters; no composition rules." value={next} onChange={(e) => setNext(e.currentTarget.value)} required autoComplete="new-password" />
          <PasswordInput label="Repeat new password" value={again} onChange={(e) => setAgain(e.currentTarget.value)} required autoComplete="new-password" />
          <Button type="submit" loading={busy} h={48}>{t('save')}</Button>
          <Text size="xs" c="dimmed">All your sessions are signed out after a password change.</Text>
        </Stack>
      </Paper>
    </Center>
  );
}
