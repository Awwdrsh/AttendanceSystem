import { Alert, Button, Checkbox, Group, Paper, Stack, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import { settings as settingsApi } from '../api';
import { useAuth } from '../auth/AuthProvider';
import { toast } from '../lib/toast';

const ROLES = [
  { value: 'admin', label: 'Administrators', hint: 'Can add users and change roles.' },
  { value: 'hr', label: 'HR', hint: 'See salaries, bank details and every record.' },
  { value: 'manager', label: 'Managers', hint: 'Approve leave and attendance for their team.' },
  { value: 'employee', label: 'Everyone else', hint: 'Their own attendance, leave and payslips.' },
];

/** Settings → Security: roles that must use two-step sign-in. */
export function SecurityPolicySettings({ value, onSaved }) {
  const { user, refresh } = useAuth();
  const [roles, setRoles] = useState(Array.isArray(value.require2faRoles) ? value.require2faRoles : []);
  const [busy, setBusy] = useState(false);
  useEffect(() => setRoles(Array.isArray(value.require2faRoles) ? value.require2faRoles : []), [value]);
  const affectsMe = !user?.twoFactorEnabled && (user?.roles ?? []).some((r) => roles.includes(r));
  return (
    <Stack gap="lg">
      <Paper withBorder p="lg">
        <Text fw={600}>Two-step sign-in</Text>
        <Text size="sm" c="dimmed" mt={4} mb="md">
          People in the roles you tick must use a code from an authenticator app after their password. Anyone not set up yet is asked to do
          it at their next sign-in before they can use Hajiri.
        </Text>
        <Checkbox.Group value={roles} onChange={setRoles}>
          <Stack gap="sm">
            {ROLES.map((r) => (
              <Checkbox key={r.value} value={r.value} label={r.label} description={r.hint} />
            ))}
          </Stack>
        </Checkbox.Group>
        {affectsMe ? (
          <Alert mt="md" color="orange" variant="light">
            This includes you. After saving you will be asked to set up two-step sign-in before continuing.
          </Alert>
        ) : null}
      </Paper>
      <Group justify="flex-end">
        <Button
          loading={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await settingsApi.put('security.policy', { require2faRoles: roles });
              toast.ok('Security settings saved.');
              onSaved();
              await refresh();
            } catch (e) {
              toast.err(e);
            } finally {
              setBusy(false);
            }
          }}
        >
          Save changes
        </Button>
      </Group>
    </Stack>
  );
}
