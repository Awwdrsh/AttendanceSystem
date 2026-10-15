import { Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { IconCircleCheck, IconFileDescription, IconMailPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { documents as docsApi, nptDateOf, type LetterRequest, type Policy } from '../../api/documents';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { EmployeeDocuments } from './EmployeeDocuments';
import { LetterTable, RequestLetterDrawer, RequestList } from './Letters';

const ICON = { size: 16, stroke: 1.6 } as const;

/** Company policies addressed to me: open, read, acknowledge. Unacknowledged ones come first. */
function MyPolicies() {
  const q = useQuery(() => docsApi.policies(), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const list = [...(q.data?.policies ?? [])].sort((a, b) => Number(!!a.my_ack_at || !a.requires_ack) - Number(!!b.my_ack_at || !b.requires_ack));
  if (!list.length) return null;
  const ack = async (p: Policy) => {
    setBusy(p.id);
    try { await docsApi.acknowledge(p.id); toast.ok(`Thanks, ${p.title} acknowledged.`); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); }
  };
  const pending = list.filter((p) => p.requires_ack && !p.my_ack_at).length;
  return (
    <div>
      <SectionTitle {...(pending ? { hint: `${pending} waiting for you to read and acknowledge` } : {})}>Company policies</SectionTitle>
      <Stack gap="xs">
        {list.map((p) => (
          <Paper key={p.id} withBorder radius="md" p="sm" style={p.requires_ack && !p.my_ack_at ? { borderColor: 'var(--mantine-color-orange-4)' } : undefined}>
            <Group justify="space-between" wrap="wrap" gap="xs">
              <Group gap={10} wrap="nowrap" style={{ minWidth: 0 }}>
                <IconFileDescription size={22} stroke={1.5} color="var(--hj-hydro)" />
                <div style={{ minWidth: 0 }}>
                  <Text size="sm" fw={600}>{p.title}</Text>
                  <Text size="xs" c="dimmed">Published {fmtDate(nptDateOf(p.published_at))}{p.description ? ` · ${p.description}` : ''}</Text>
                </div>
              </Group>
              <Group gap={6}>
                <Button size="compact-sm" variant="default" component="a" href={docsApi.policyUrl(p.id)} target="_blank" rel="noopener"
                  onClick={() => setOpened((s) => new Set(s).add(p.id))}>Read</Button>
                {p.my_ack_at ? <Badge variant="light" color="brand" leftSection={<IconCircleCheck size={12} />}>Acknowledged {fmtDate(nptDateOf(p.my_ack_at))}</Badge>
                  : p.requires_ack ? <Button size="compact-sm" loading={busy === p.id} disabled={!opened.has(p.id)} title={opened.has(p.id) ? 'Confirm you have read it' : 'Open the document first'} onClick={() => ack(p)}>I have read this</Button> : null}
              </Group>
            </Group>
          </Paper>
        ))}
      </Stack>
    </div>
  );
}

/** Profile → Documents & letters: my documents, letters from HR, letter requests and company policies. */
export default function MyDocumentsPage() {
  const { user } = useAuth();
  const letters = useQuery(() => docsApi.letters(), []);
  const reqs = useQuery(() => docsApi.requests(), []);
  const [asking, setAsking] = useState(false);
  const [cancelling, setCancelling] = useState<LetterRequest | null>(null);
  const open = (reqs.data?.requests ?? []).filter((r) => r.status === 'pending' || r.status === 'approved' || r.status === 'rejected').slice(0, 6);
  return (
    <Stack gap="xl">
      <MyPolicies />
      <div>
        <SectionTitle>My documents</SectionTitle>
        <EmployeeDocuments employeeId={user?.employeeId ?? null} mode="self" />
      </div>
      <div>
        <SectionTitle actions={<Button size="xs" variant="light" leftSection={<IconMailPlus {...ICON} />} onClick={() => setAsking(true)}>Request a letter</Button>}>Letters from HR</SectionTitle>
        <Stack gap="md">
          {open.length ? <RequestList requests={open} onCancel={(r) => setCancelling(r)} /> : null}
          <LetterTable letters={letters.data?.letters ?? []} loading={letters.loading} error={letters.error} />
        </Stack>
      </div>
      <RequestLetterDrawer opened={asking} onClose={() => setAsking(false)} onDone={() => { void reqs.reload(); }} />
      <ConfirmDialog opened={!!cancelling} title="Cancel request?" confirmLabel="Cancel request" message={cancelling ? `Your request for a ${cancelling.template_name.toLowerCase()} will be withdrawn.` : ''}
        onConfirm={async () => { if (!cancelling) return; try { await docsApi.cancelRequest(cancelling.id); toast.ok('Request cancelled.'); await reqs.reload(); } catch (e) { toast.err(e); } }}
        onClose={() => setCancelling(null)} />
    </Stack>
  );
}
