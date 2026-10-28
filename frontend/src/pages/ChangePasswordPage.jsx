import { Alert, Anchor, Button, Center, Group, Paper, PasswordInput, Stack, Text, Title } from '@mantine/core';
import { IconAlertCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { auth as authApi } from '../api';
import { errorMessage } from '../api/client';
import { Brand } from '../components/Brand';
import { useAuth } from '../auth/AuthProvider';
import { toast } from '../lib/toast';

export default function ChangePasswordPage() {
  const { user, refresh } = useAuth();
  const nav = useNavigate();
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    if (next !== again) {
      setError('The two new passwords do not match. Type the same password in both fields.');
      return;
    }
    if (next.length < 12) {
      setError('Use at least 12 characters. A short sentence works well.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await authApi.changePassword(cur, next);
      toast.ok('Password changed. Please sign in again.');
      await refresh();
      nav('/login', { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Center mih="100vh" p="md" bg="var(--hj-canvas)">
      <Stack w="100%" maw={400} gap="lg">
        <Group justify="center">
          <Brand height={24} />
        </Group>
        <Paper withBorder p="xl" bg="var(--hj-surface)" component="form" onSubmit={submit}>
          <Stack>
            <div>
              <Title order={2} fz={20} fw={600} style={{ letterSpacing: '-0.01em' }}>
                {user?.mustChangePassword ? 'Choose a new password' : 'Change password'}
              </Title>
              <Text size="sm" c="dimmed" mt={4}>
                {user?.mustChangePassword
                  ? 'Your account needs a new password before you continue.'
                  : 'You will be signed out everywhere and asked to sign in again.'}
              </Text>
            </div>
            {error ? (
              <Alert color="red" variant="light" icon={<IconAlertCircle size={16} stroke={1.6} />}>
                {error}
              </Alert>
            ) : null}
            <PasswordInput
              label="Current password"
              value={cur}
              onChange={(e) => setCur(e.currentTarget.value)}
              required
              autoComplete="current-password"
            />
            <PasswordInput
              label="New password"
              description="At least 12 characters. No other rules."
              value={next}
              onChange={(e) => setNext(e.currentTarget.value)}
              required
              autoComplete="new-password"
            />
            <PasswordInput
              label="Repeat new password"
              value={again}
              onChange={(e) => setAgain(e.currentTarget.value)}
              required
              autoComplete="new-password"
            />
            <Button type="submit" loading={busy} fullWidth mt="xs">
              Change password
            </Button>
          </Stack>
        </Paper>
        {!user?.mustChangePassword ? (
          <Anchor component={Link} to="/" size="sm" c="dimmed" ta="center">
            Back to Hajiri
          </Anchor>
        ) : null}
      </Stack>
    </Center>
  );
}
