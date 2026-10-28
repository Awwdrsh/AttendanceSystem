import { Anchor, Center, Group, Paper, Stack, Text, Title } from '@mantine/core';
import { Navigate, useNavigate } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { Brand } from '../../components/Brand';
import { TwoFactorSetup } from '../../components/TwoFactorSetup';
import { toast } from '../../lib/toast';

/** The company requires two-step sign-in for this role: set it up before using Hajiri. */
export default function TwoStepSetupPage() {
  const { user, refresh, logout } = useAuth();
  const nav = useNavigate();
  if (user && !user.mustSetUpTwoFactor) return <Navigate to="/" replace />;
  return (
    <Center mih="100vh" p="md" bg="var(--hj-canvas)">
      <Stack w="100%" maw={560} gap="lg">
        <Group justify="center">
          <Brand height={24} />
        </Group>
        <Paper withBorder p="xl" bg="var(--hj-surface)">
          <Stack>
            <div>
              <Title order={2} fz={20} fw={600} style={{ letterSpacing: '-0.01em' }}>
                Set up two-step sign-in
              </Title>
              <Text size="sm" c="dimmed" mt={4}>
                CoreValley requires a second step for your role. It takes a minute and protects salaries and personal records if your
                password is ever leaked.
              </Text>
            </div>
            <TwoFactorSetup
              onEnabled={async () => {
                await refresh();
                toast.ok('Two-step sign-in is on.');
                nav('/', { replace: true });
              }}
            />
          </Stack>
        </Paper>
        <Anchor
          component="button"
          size="sm"
          c="dimmed"
          ta="center"
          onClick={async () => {
            await logout();
            nav('/login');
          }}
        >
          Sign out
        </Anchor>
      </Stack>
    </Center>
  );
}
