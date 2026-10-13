import { Badge, Button, Group, List, Paper, ScrollArea, SimpleGrid, Stack, Table, Tabs, Text } from '@mantine/core';
import { IconDownload } from '@tabler/icons-react';
import { useState } from 'react';
import { attendance as attApi, biometrics as bioApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { addDays, fmtAd, fmtDateShort, fmtTime, NPT_OFFSET_MIN, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { sourceLabel } from './HomePage';

const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ') : s);
const nptDate = (iso: string) => fmtAd(new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString());

/** Transparency page: what is stored, who sees it, how to withdraw; data export; own clock-in log. */
export default function MyDataPage() {
  const { user } = useAuth();
  const employeeId = user?.employeeId ?? '';
  const tr = useQuery(() => bioApi.transparency(), []);
  const consents = useQuery(() => bioApi.consents(employeeId), [employeeId], { enabled: !!employeeId });
  const templates = useQuery(() => bioApi.templates(employeeId), [employeeId], { enabled: !!employeeId });
  const punches = useQuery(() => attApi.punches(employeeId, addDays(todayNpt(), -14), todayNpt()), [employeeId], { enabled: !!employeeId });
  const [revoke, setRevoke] = useState<string | null>(null);
  const consentRows = consents.data?.consents ?? [];
  const punchRows = punches.data?.punches ?? [];
  const activeTemplates = (templates.data?.templates ?? []).filter((x) => !x.shredded_at).length;

  return (
    <Stack gap="lg">
      <PageHeader title="My data and privacy" subtitle="What Hajiri stores about you, who can see it, and how to take it with you."
        actions={employeeId ? <Button variant="default" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={() => bioApi.dsar(employeeId).catch((e) => toast.err(e, 'Download failed'))}>Download my data</Button> : null} />

      <Tabs defaultValue="privacy">
        <Tabs.List mb="md">
          <Tabs.Tab value="privacy">What we store</Tabs.Tab>
          <Tabs.Tab value="consent">Biometric consent</Tabs.Tab>
          <Tabs.Tab value="log">Clock-in log</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="privacy">
          {tr.data ? (
            <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
              <Paper withBorder p="lg">
                <Text fw={600}>Stored</Text>
                <List size="sm" mt="xs" spacing={4}>{tr.data.stored.en.map((s) => <List.Item key={s}>{s}</List.Item>)}</List>
                <Text fw={600} mt="lg">Never stored</Text>
                <List size="sm" mt="xs" spacing={4}>{tr.data.notStored.en.map((s) => <List.Item key={s}>{s}</List.Item>)}</List>
              </Paper>
              <Paper withBorder p="lg">
                <Text fw={600}>Who can see it</Text>
                <List size="sm" mt="xs" spacing={4}>{tr.data.access.en.map((s) => <List.Item key={s}>{s}</List.Item>)}</List>
                <Text fw={600} mt="lg">Withdrawing consent</Text>
                <Text size="sm" mt="xs">{tr.data.withdraw.en}</Text>
                <Text size="xs" c="dimmed" mt="md">{tr.data.legalBasis.en}</Text>
              </Paper>
            </SimpleGrid>
          ) : tr.error ? <EmptyState title="Could not load the privacy notice" hint={tr.error} /> : null}
        </Tabs.Panel>

        <Tabs.Panel value="consent">
          <Stack gap="md">
            {consentRows.length === 0 ? (
              <EmptyState title="No consent recorded" hint="HR records your consent when they enrol your fingerprint or face on a terminal." />
            ) : (
              <Paper withBorder style={{ overflow: 'hidden' }}>
                {consentRows.map((c, i) => (
                  <Group key={c.id} justify="space-between" wrap="wrap" px="lg" py="sm" style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
                    <div>
                      <Text size="sm" fw={500}>{cap(c.modality)}, {c.purpose}</Text>
                      <Text size="xs" c="dimmed">Granted {nptDate(c.granted_at)}, notice version {c.notice_version}{c.revoked_at ? `. Withdrawn ${nptDate(c.revoked_at)}` : ''}</Text>
                    </div>
                    <Group gap="sm">
                      <StatusBadge status={c.revoked_at ? 'cancelled' : 'active'} />
                      {!c.revoked_at ? <Button size="xs" variant="subtle" color="red" onClick={() => setRevoke(c.id)}>Withdraw consent</Button> : null}
                    </Group>
                  </Group>
                ))}
              </Paper>
            )}
            <Text size="sm" c="dimmed">Biometric templates stored for you: <Text span fw={600} c="var(--mantine-color-text)" className="num">{activeTemplates}</Text></Text>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="log">
          <Text size="sm" c="dimmed" mb="sm">Every clock-in and clock-out recorded for you in the last 14 days, including replaced entries.</Text>
          {punchRows.length === 0 ? <EmptyState title="Nothing recorded in the last 14 days" /> : (
            <Paper withBorder style={{ overflow: 'hidden' }}>
              <ScrollArea type="auto">
                <Table miw={560}>
                  <Table.Thead><Table.Tr><Table.Th>Date</Table.Th><Table.Th style={{ textAlign: 'right' }}>Time</Table.Th><Table.Th>Entry</Table.Th><Table.Th>Method</Table.Th><Table.Th>Source</Table.Th><Table.Th>Device</Table.Th></Table.Tr></Table.Thead>
                  <Table.Tbody>
                    {punchRows.map((p) => (
                      <Table.Tr key={p.id} {...(p.superseded ? { c: 'dimmed' } : {})}>
                        <Table.Td>{fmtDateShort(p.local_date)}</Table.Td>
                        <Table.Td ta="right" className="num">{fmtTime(p.punched_at)}</Table.Td>
                        <Table.Td>{p.direction === 'in' ? 'Clock-in' : p.direction === 'out' ? 'Clock-out' : 'Unpaired'}{p.superseded ? <Badge ml={6} size="sm" variant="light" color="gray">Replaced</Badge> : null}</Table.Td>
                        <Table.Td>{cap(p.verify_mode)}</Table.Td>
                        <Table.Td>{sourceLabel(p.source)}</Table.Td>
                        <Table.Td c="dimmed">{p.device_sn ?? '—'}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </ScrollArea>
            </Paper>
          )}
        </Tabs.Panel>
      </Tabs>

      <ConfirmDialog opened={!!revoke} onClose={() => setRevoke(null)} title="Withdraw consent?" confirmLabel="Withdraw consent"
        message="Your stored templates for this method are deleted immediately. You'll clock in with a card or PIN afterwards."
        onConfirm={async () => { if (!revoke) return; try { const r = await bioApi.revoke(revoke); toast.ok(`Consent withdrawn. ${r.templatesShredded} template(s) deleted.`); await Promise.all([consents.reload(), templates.reload()]); } catch (e) { toast.err(e, 'Could not withdraw consent'); } }} />
    </Stack>
  );
}
