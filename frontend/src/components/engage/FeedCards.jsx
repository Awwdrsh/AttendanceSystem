import { Avatar, Badge, Button, Group, Menu, ActionIcon, Modal, Paper, Progress, Stack, Table, Text } from '@mantine/core';
import { IconCheck, IconDots, IconPin, IconSpeakerphone, IconTrash, IconUsers } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { engage } from '../../api/engage';
import { fmtDate, fmtDateShort, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { SocialBar } from './Social';

export const initials = (name) =>
  name
    .split(/\s+/)
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();

export function KudosCard({ k, onRemoved }) {
  return (
    <Paper withBorder p="md" radius="lg">
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
        <Group gap="sm" wrap="nowrap" align="flex-start" style={{ minWidth: 0 }}>
          <Avatar radius="xl" color="brand" size={38}>
            {initials(k.giver.name)}
          </Avatar>
          <div style={{ minWidth: 0 }}>
            <Text size="sm">
              <Text span fw={600}>
                {k.giver.name}
              </Text>{' '}
              gave kudos to{' '}
              <Text span fw={600}>
                {k.recipients.map((r) => r.name).join(', ')}
              </Text>
            </Text>
            <Text size="xs" c="dimmed">
              {relativeTime(k.createdAt)}
            </Text>
          </div>
        </Group>
        {k.canDelete ? (
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" color="gray" aria-label="Kudos options">
                <IconDots size={16} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item
                color="red"
                leftSection={<IconTrash size={14} />}
                onClick={async () => {
                  try {
                    await engage.deleteKudos(k.id);
                    toast.ok('Kudos removed.');
                    onRemoved();
                  } catch (e) {
                    toast.err(e);
                  }
                }}
              >
                Remove
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        ) : null}
      </Group>
      <Stack gap="sm" mt="sm" pl={{ base: 0, xs: 50 }}>
        {k.value ? (
          <Badge variant="light" color="brand" size="lg" radius="sm" w="fit-content" leftSection={<span aria-hidden>{k.value.emoji}</span>}>
            {k.value.name}
          </Badge>
        ) : null}
        <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {k.message}
        </Text>
        <SocialBar targetType="kudos" targetId={k.id} reactions={k.reactions} mine={k.mine} comments={k.comments} />
      </Stack>
    </Paper>
  );
}

function AcksModal({ id, opened, onClose }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!opened) return;
    engage
      .acks(id)
      .then(setData)
      .catch((e) => {
        toast.err(e);
        onClose();
      });
  }, [opened, id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Modal
      opened={opened}
      onClose={() => {
        setData(null);
        onClose();
      }}
      title="Read confirmations"
      centered
      size="lg"
    >
      {data ? (
        <Stack>
          <Group justify="space-between">
            <Text size="sm">
              {data.acked} of {data.audience} confirmed
            </Text>
            <Text size="sm" c="dimmed">
              {data.audience ? Math.round((data.acked / data.audience) * 100) : 0}%
            </Text>
          </Group>
          <Progress value={data.audience ? (data.acked / data.audience) * 100 : 0} />
          <Table.ScrollContainer minWidth={360} mah={380}>
            <Table fz="sm" verticalSpacing={6}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Name</Table.Th>
                  <Table.Th>Code</Table.Th>
                  <Table.Th>Confirmed</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.people.map((p) => (
                  <Table.Tr key={p.employeeId}>
                    <Table.Td>{p.name}</Table.Td>
                    <Table.Td className="num">{p.empCode}</Table.Td>
                    <Table.Td>
                      {p.ackedAt ? (
                        <Group gap={4}>
                          <IconCheck size={14} color="var(--hj-ok)" />
                          {fmtDate(p.ackedAt.slice(0, 10))}
                        </Group>
                      ) : (
                        <Text size="sm" c="dimmed">
                          Not yet
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          Loading…
        </Text>
      )}
    </Modal>
  );
}

export function AnnouncementCard({ a, onChanged }) {
  const [acked, setAcked] = useState(a.acked);
  const [acksOpen, setAcksOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <Paper withBorder p="md" radius="lg" style={a.pinned ? { borderColor: 'var(--hj-hydro)' } : undefined}>
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
        <Group gap="sm" wrap="nowrap" align="flex-start" style={{ minWidth: 0 }}>
          <Avatar radius="xl" color="blue" size={38}>
            <IconSpeakerphone size={18} stroke={1.6} />
          </Avatar>
          <div style={{ minWidth: 0 }}>
            <Group gap={6} wrap="wrap">
              <Text fw={600}>{a.title}</Text>
              {a.pinned ? (
                <Badge size="xs" variant="light" leftSection={<IconPin size={10} />}>
                  Pinned
                </Badge>
              ) : null}
            </Group>
            <Text size="xs" c="dimmed">
              {a.authorName} · {a.audienceLabel} · {relativeTime(a.createdAt)}
              {a.expiresOn ? ` · until ${fmtDateShort(a.expiresOn)}` : ''}
            </Text>
          </div>
        </Group>
        {a.canEdit ? (
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" color="gray" aria-label="Announcement options">
                <IconDots size={16} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              {a.requiresAck ? (
                <Menu.Item leftSection={<IconUsers size={14} />} onClick={() => setAcksOpen(true)}>
                  Who has read it
                </Menu.Item>
              ) : null}
              <Menu.Item
                leftSection={<IconPin size={14} />}
                onClick={async () => {
                  try {
                    await engage.editAnnouncement(a.id, { pinned: !a.pinned });
                    onChanged();
                  } catch (e) {
                    toast.err(e);
                  }
                }}
              >
                {a.pinned ? 'Unpin' : 'Pin to top'}
              </Menu.Item>
              <Menu.Item
                color="red"
                leftSection={<IconTrash size={14} />}
                onClick={async () => {
                  try {
                    await engage.deleteAnnouncement(a.id);
                    toast.ok('Announcement removed.');
                    onChanged();
                  } catch (e) {
                    toast.err(e);
                  }
                }}
              >
                Remove
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
        ) : null}
      </Group>
      <Stack gap="sm" mt="sm" pl={{ base: 0, xs: 50 }}>
        <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {a.body}
        </Text>
        {a.requiresAck ? (
          acked ? (
            <Group gap={6}>
              <IconCheck size={16} color="var(--hj-ok)" />
              <Text size="sm" c="dimmed">
                You confirmed you have read this.
              </Text>
            </Group>
          ) : (
            <Button
              size="xs"
              w="fit-content"
              loading={busy}
              leftSection={<IconCheck size={14} />}
              onClick={async () => {
                setBusy(true);
                try {
                  await engage.ack(a.id);
                  setAcked(true);
                } catch (e) {
                  toast.err(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              I have read this
            </Button>
          )
        ) : null}
        <SocialBar targetType="announcement" targetId={a.id} reactions={a.reactions} mine={a.mine} comments={a.comments} compact />
      </Stack>
      {a.canEdit ? <AcksModal id={a.id} opened={acksOpen} onClose={() => setAcksOpen(false)} /> : null}
    </Paper>
  );
}
