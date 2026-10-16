import { Alert, Badge, Button, Group, Paper, Stack, Text, TextInput } from '@mantine/core';
import { IconDeviceMobile } from '@tabler/icons-react';
import { useState } from 'react';
import { relations } from '../../api/relations';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { deviceToken, existingDeviceToken } from '../../lib/deviceToken';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS = { pending: ['Waiting for HR', 'orange'], approved: ['Approved', 'teal'], rejected: ['Not approved', 'red'], revoked: ['Removed', 'gray'] } as const;

function guessLabel(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/Android/.test(ua)) return 'Android phone';
  if (/Macintosh/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows computer';
  return 'My device';
}

/** Register this phone or computer for clock-in, when the company requires registered devices. */
export default function MyDevicesPage() {
  const q = useQuery(() => relations.myDevices(), []);
  const [label, setLabel] = useState(guessLabel());
  const [busy, setBusy] = useState(false);
  const thisRegistered = !!existingDeviceToken() && (q.data?.devices ?? []).length > 0;
  return (
    <Stack gap="lg">
      <PageHeader title="Clock-in devices" subtitle="Phones and computers you may clock in from. HR approves each one." />
      {q.data && !q.data.required ? <Alert variant="light" color="gray">Your company does not require registered devices at the moment. You can still register this device in advance.</Alert> : null}
      {q.data?.required ? <Alert variant="light" color="orange">Your company requires a registered device to clock in from the website or app.</Alert> : null}
      <Paper withBorder p="md">
        <Stack gap="sm">
          <Group gap="xs"><IconDeviceMobile size={18} stroke={1.6} /><Text fw={600}>Register this device</Text></Group>
          <Group align="flex-end" wrap="wrap">
            <TextInput label="Name" value={label} onChange={(e) => setLabel(e.currentTarget.value)} maxLength={80} style={{ flex: 1, minWidth: 200 }} />
            <Button loading={busy} disabled={!label.trim()} onClick={async () => {
              setBusy(true);
              try { await relations.registerDevice(deviceToken(), label.trim()); toast.ok('Registered. HR has been asked to approve it.'); await q.reload(); }
              catch (e) { toast.err(e); } finally { setBusy(false); }
            }}>Register</Button>
          </Group>
          {thisRegistered ? <Text size="xs" c="dimmed">This browser already holds a device key. Registering again only works if the old one was removed.</Text> : null}
          <Text size="xs" c="dimmed">The key stays in this browser. Clearing the browser's data removes it and you will need to register again.</Text>
        </Stack>
      </Paper>
      {!q.loading && !(q.data?.devices ?? []).length ? <EmptyState compact title="No devices yet" /> : null}
      {(q.data?.devices ?? []).map((d) => (
        <Paper key={d.id} withBorder p="sm">
          <Group justify="space-between" wrap="nowrap">
            <div><Text size="sm" fw={500}>{d.label}</Text><Text size="xs" c="dimmed">Registered {relativeTime(d.requested_at)}{d.last_used_at ? `, last clock-in ${relativeTime(d.last_used_at)}` : ''}</Text></div>
            <Group gap="xs" wrap="nowrap"><Badge variant="light" color={STATUS[d.status][1]}>{STATUS[d.status][0]}</Badge>
              <Button size="compact-xs" variant="subtle" color="gray" onClick={async () => { try { await relations.removeDevice(d.id); toast.ok('Removed.'); await q.reload(); } catch (e) { toast.err(e); } }}>Remove</Button></Group>
          </Group>
        </Paper>
      ))}
    </Stack>
  );
}
