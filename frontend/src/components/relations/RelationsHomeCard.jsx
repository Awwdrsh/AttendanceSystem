import { Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconUserCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { KYE_SECTION_LABEL, relations } from '../../api/relations';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Home: a profile review HR asked for, and any notice from HR waiting for a response. Renders nothing otherwise. */
export function RelationsHomeCard() {
  const kye = useQuery(() => relations.myKye(), []);
  const cases = useQuery(() => relations.myCases(), []);
  const [busy, setBusy] = useState(false);
  const pending = (kye.data?.campaigns ?? []).filter((c) => c.status === 'pending');
  const notices = (cases.data?.cases ?? []).filter((c) => c.status === 'show_cause');
  if (!pending.length && !notices.length) return null;
  const missing = kye.data?.completeness?.missing ?? [];
  return (
    <Stack gap="sm">
      {notices.map((k) => (
        <Paper key={k.id} withBorder p="md" style={{ borderColor: 'var(--mantine-color-orange-5)' }}>
          <Group justify="space-between" wrap="nowrap">
            <Group gap="sm" wrap="nowrap">
              <IconAlertTriangle size={20} stroke={1.6} color="var(--mantine-color-orange-6)" />
              <div>
                <Text fw={600} size="sm">
                  HR has asked you to respond
                </Text>
                <Text size="xs" c="dimmed">
                  Reply by {fmtAd(k.responseDue)}
                </Text>
              </div>
            </Group>
            <Button size="xs" component={Link} to="/me/profile/conduct">
              Respond
            </Button>
          </Group>
        </Paper>
      ))}
      {pending.map((c) => (
        <Paper key={c.id} withBorder p="md">
          <Stack gap="xs">
            <Group justify="space-between" wrap="nowrap">
              <Group gap="sm" wrap="nowrap">
                <IconUserCheck size={20} stroke={1.6} />
                <div>
                  <Text fw={600} size="sm">
                    {c.title}
                  </Text>
                  <Text size="xs" c="dimmed">
                    Please check your details by {fmtAd(c.due_date)}
                  </Text>
                </div>
              </Group>
            </Group>
            {c.message ? <Text size="sm">{c.message}</Text> : null}
            <Group gap={4}>
              {c.sections.map((s) => (
                <Badge key={s} size="xs" variant="light" color={missing.includes(s) ? 'orange' : 'gray'}>
                  {KYE_SECTION_LABEL[s]}
                  {missing.includes(s) ? ': missing' : ''}
                </Badge>
              ))}
            </Group>
            <Group gap="xs">
              <Button size="xs" variant="default" component={Link} to="/me/profile">
                Review my profile
              </Button>
              <Button
                size="xs"
                loading={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await relations.respondKye(c.id, 'confirmed');
                    toast.ok('Thank you. HR has your confirmation.');
                    await kye.reload();
                  } catch (e) {
                    toast.err(e);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                My details are correct
              </Button>
              <Button
                size="xs"
                variant="subtle"
                component={Link}
                to="/me/profile"
                onClick={() => {
                  void relations
                    .respondKye(c.id, 'changes_requested')
                    .then(() => kye.reload())
                    .catch(() => undefined);
                }}
              >
                Something needs changing
              </Button>
            </Group>
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
}
