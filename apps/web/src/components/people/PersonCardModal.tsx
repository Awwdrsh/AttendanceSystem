import { Anchor, Badge, Button, Group, Loader, Modal, SimpleGrid, Stack, Text, UnstyledButton } from '@mantine/core';
import { IconMail, IconPhone, IconUser } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { people } from '../../api/people';
import { useAuth } from '../../auth/AuthProvider';
import { fmtAd } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';
import { Field } from '../Kpi';
import { PersonAvatar } from './PersonAvatar';

/** A colleague's work card: who they are, how to reach them, who they report to and who reports to them. */
export function PersonCardModal({ id, onClose }: { id: string | null; onClose: () => void }) {
  const [current, setCurrent] = useState(id);
  useEffect(() => setCurrent(id), [id]);
  const { hasRole } = useAuth();
  const q = useQuery(() => people.card(current!), [current], { enabled: !!current });
  const p = q.data?.person;
  return (
    <Modal opened={!!current} onClose={onClose} title="Profile" size="lg" centered>
      {q.loading && !p ? <Group justify="center" p="xl"><Loader size="sm" /></Group> : null}
      {q.error ? <Text c="red" size="sm">{q.error}</Text> : null}
      {p && current ? (
        <Stack gap="lg">
          <Group gap="md" wrap="nowrap">
            <PersonAvatar id={p.id} name={p.name_en} hasAvatar={p.has_avatar} size={64} />
            <div style={{ minWidth: 0 }}>
              <Text fw={650} size="lg" truncate>{p.name_en}</Text>
              <Text size="sm" c="dimmed" truncate>{[p.designation, p.unit_name].filter(Boolean).join(', ') || '—'}</Text>
              {p.status !== 'active' ? <Badge size="sm" color="gray" variant="light" mt={4}>Left the company</Badge> : null}
            </div>
          </Group>
          <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="md">
            <Field label="Work email">{p.work_email ? <Anchor href={`mailto:${p.work_email}`} size="sm"><IconMail size={14} stroke={1.6} style={{ verticalAlign: -2, marginRight: 4 }} />{p.work_email}</Anchor> : '—'}</Field>
            <Field label="Work phone">{p.work_phone ? <Anchor href={`tel:${p.work_phone}`} size="sm"><IconPhone size={14} stroke={1.6} style={{ verticalAlign: -2, marginRight: 4 }} />{p.work_phone}</Anchor> : '—'}</Field>
            <Field label="Branch">{p.branch_name ?? '—'}</Field>
            <Field label="Joined">{fmtAd(p.join_date)}</Field>
            <Field label="Employee code">{p.emp_code}</Field>
            <Field label="Reports to">
              {p.manager_id ? <Anchor component="button" size="sm" onClick={() => setCurrent(p.manager_id)}>{p.manager_name}</Anchor> : '—'}
            </Field>
          </SimpleGrid>
          {p.skills.length || p.languages.length ? (
            <div>
              <Text size="xs" c="dimmed" mb={6}>Skills and languages</Text>
              <Group gap={6}>
                {p.skills.map((s) => <Badge key={`s-${s}`} variant="light" color="brand" size="sm">{s}</Badge>)}
                {p.languages.map((s) => <Badge key={`l-${s}`} variant="outline" color="gray" size="sm">{s}</Badge>)}
              </Group>
            </div>
          ) : null}
          {q.data?.reports.length ? (
            <div>
              <Text size="xs" c="dimmed" mb={6}>Direct reports ({q.data.reports.length})</Text>
              <Stack gap={4}>
                {q.data.reports.map((r) => (
                  <UnstyledButton key={r.id} onClick={() => setCurrent(r.id)} style={{ borderRadius: 6, padding: 4 }}>
                    <Group gap="sm" wrap="nowrap">
                      <PersonAvatar id={r.id} name={r.name_en} hasAvatar={r.has_avatar} size={28} />
                      <Text size="sm" truncate>{r.name_en}</Text>
                      <Text size="xs" c="dimmed" truncate>{r.designation ?? ''}</Text>
                    </Group>
                  </UnstyledButton>
                ))}
              </Stack>
            </div>
          ) : null}
          {hasRole('hr') ? (
            <Group justify="flex-end">
              <Button component={Link} to={`/employees/${p.id}`} variant="default" leftSection={<IconUser size={16} stroke={1.6} />} onClick={onClose}>Open employee record</Button>
            </Group>
          ) : null}
        </Stack>
      ) : null}
    </Modal>
  );
}
