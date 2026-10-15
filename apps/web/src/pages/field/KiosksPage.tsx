import { ActionIcon, Alert, Badge, Button, Code, CopyButton, Group, Menu, Modal, Select, Stack, Switch, Text, TextInput } from '@mantine/core';
import { IconDeviceTablet, IconDots, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { settings as settingsApi } from '../../api';
import { field as fieldApi, type KioskRow } from '../../api/field';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, fmtTime, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

interface KForm { id?: string; name: string; branchId: string | null; locationId: string | null; selfieRequired: boolean }

function statusBadge(k: KioskRow) {
  if (k.status === 'revoked') return <Badge color="gray" variant="light">Revoked</Badge>;
  if (k.locked) return <Badge color="red" variant="light">Paused</Badge>;
  if (k.status === 'pairing') return <Badge color="yellow" variant="light">Waiting to pair</Badge>;
  const online = k.last_seen_at && Date.now() - Date.parse(k.last_seen_at) < 10 * 60_000;
  return <Badge color={online ? 'teal' : 'gray'} variant="light">{online ? 'Online' : 'Idle'}</Badge>;
}

/** HR: shared clock-in tablets. Each pairs once with a one-time code; employees punch with their code and PIN or QR badge. */
export default function KiosksPage() {
  const q = useQuery(() => fieldApi.kiosks(), []);
  const branches = useQuery(() => settingsApi.branches(), []);
  const locs = useQuery(() => fieldApi.locations(), []);
  const [form, setForm] = useState<KForm | null>(null);
  const [code, setCode] = useState<{ name: string; pairingCode: string; expiresAt: string } | null>(null);
  const [revoke, setRevoke] = useState<KioskRow | null>(null);
  const [busy, setBusy] = useState(false);
  const kioskUrl = `${window.location.origin}/kiosk`;

  const save = async () => {
    if (!form) return;
    setBusy(true);
    try {
      const body = { name: form.name.trim(), branchId: form.branchId, locationId: form.locationId, selfieRequired: form.selfieRequired };
      if (form.id) { await fieldApi.updateKiosk(form.id, body); toast.ok('Kiosk saved.'); }
      else { const r = await fieldApi.createKiosk(body); setCode({ name: body.name, pairingCode: r.pairingCode, expiresAt: r.expiresAt }); }
      setForm(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const act = async (fn: () => Promise<unknown>, msg: string) => { try { await fn(); toast.ok(msg); await q.reload(); } catch (e) { toast.err(e); } };

  return (
    <Stack gap="lg">
      <SectionTitle hint={<>Turn a tablet at the entrance into a clock-in station. Open <Code>{kioskUrl}</Code> on the tablet and enter the pairing code.</>}
        actions={<Button size="sm" leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setForm({ name: '', branchId: null, locationId: null, selfieRequired: true })}>Add kiosk</Button>}>Kiosks</SectionTitle>
      <DataTable rows={q.data?.kiosks ?? []} rowKey={(k) => k.id} loading={q.loading} error={q.error} emptyTitle="No kiosks yet"
        emptyHint="Add a kiosk, then open the kiosk page on a tablet and pair it with the code."
        columns={[
          { key: 'name', header: 'Kiosk', sticky: true, render: (k) => <Group gap="sm" wrap="nowrap"><IconDeviceTablet size={18} stroke={1.5} /><div><Text size="sm" fw={500}>{k.name}</Text><Text size="xs" c="dimmed">{[k.branch_name, k.location_name].filter(Boolean).join(' · ') || 'Any branch'}</Text></div></Group> },
          { key: 'status', header: 'Status', render: statusBadge },
          { key: 'seen', header: 'Last seen', render: (k) => <Text size="sm" c="dimmed">{k.last_seen_at ? relativeTime(k.last_seen_at) : 'Never'}</Text> },
          { key: 'today', header: 'Punches today', align: 'right', render: (k) => <span className="num">{k.punches_today}</span> },
          { key: 'selfie', header: 'Photo', render: (k) => <Text size="sm" c="dimmed">{k.selfie_required ? 'Required' : 'Off'}</Text> },
          { key: 'paired', header: 'Paired', render: (k) => <Text size="sm" c="dimmed">{k.paired_at ? fmtDateShort(k.paired_at.slice(0, 10)) : '—'}</Text> },
          { key: 'act', header: '', align: 'right', render: (k) => k.status === 'revoked' ? null : <Menu position="bottom-end">
            <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label="Kiosk actions"><IconDots size={16} /></ActionIcon></Menu.Target>
            <Menu.Dropdown>
              <Menu.Item onClick={() => setForm({ id: k.id, name: k.name, branchId: k.branch_id, locationId: k.location_id, selfieRequired: k.selfie_required })}>Edit</Menu.Item>
              <Menu.Item onClick={async () => { try { const r = await fieldApi.pairingCode(k.id); setCode({ name: k.name, ...r }); await q.reload(); } catch (e) { toast.err(e); } }}>New pairing code</Menu.Item>
              {k.locked ? <Menu.Item onClick={() => act(() => fieldApi.unlockKiosk(k.id), 'Kiosk unpaused.')}>Unpause</Menu.Item> : null}
              <Menu.Divider />
              <Menu.Item color="red" onClick={() => setRevoke(k)}>Revoke</Menu.Item>
            </Menu.Dropdown>
          </Menu> },
        ]} />
      <Text size="xs" c="dimmed">A kiosk pauses for a few minutes after many wrong PINs in a row, and a person's PIN locks for 15 minutes after 5 wrong tries. Reset PINs under Settings, Locations.</Text>

      <Modal opened={!!form} onClose={() => setForm(null)} title={form?.id ? 'Edit kiosk' : 'Add kiosk'} centered>
        {form ? (
          <Stack>
            <TextInput label="Name" placeholder="Front desk tablet" value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} required maxLength={80} />
            <Select label="Branch" data={(branches.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }))} value={form.branchId} onChange={(v) => setForm({ ...form, branchId: v })} clearable placeholder="Any branch" />
            <Select label="Location" description="Punches at this kiosk count as at this location." data={(locs.data?.locations ?? []).map((l) => ({ value: l.id, label: l.name }))} value={form.locationId} onChange={(v) => setForm({ ...form, locationId: v })} clearable placeholder="None" />
            <Switch label="Take a photo with every punch" description="Stops one person punching for another. Photos are encrypted and deleted after the retention period." checked={form.selfieRequired} onChange={(e) => setForm({ ...form, selfieRequired: e.currentTarget.checked })} />
            <Group justify="flex-end"><Button variant="default" onClick={() => setForm(null)}>Cancel</Button><Button loading={busy} disabled={!form.name.trim()} onClick={save}>{form.id ? 'Save' : 'Add and get code'}</Button></Group>
          </Stack>
        ) : null}
      </Modal>
      <Modal opened={!!code} onClose={() => setCode(null)} title={code ? `Pair ${code.name}` : ''} centered>
        {code ? (
          <Stack align="center" gap="md">
            <Text size="sm" ta="center">On the tablet, open <Code>{kioskUrl}</Code> and enter this code:</Text>
            <Text className="num" fz={36} fw={700} style={{ letterSpacing: '0.12em' }}>{code.pairingCode}</Text>
            <CopyButton value={code.pairingCode}>{({ copied, copy }) => <Button variant="default" size="xs" onClick={copy}>{copied ? 'Copied' : 'Copy code'}</Button>}</CopyButton>
            <Alert variant="light" color="yellow" w="100%">The code works once and expires at {fmtTime(code.expiresAt)}. It is not shown again.</Alert>
          </Stack>
        ) : null}
      </Modal>
      <ConfirmDialog opened={!!revoke} title="Revoke kiosk" message={revoke ? `${revoke.name} will stop working straight away. To use the tablet again, add a new kiosk.` : ''} confirmLabel="Revoke"
        onClose={() => setRevoke(null)} onConfirm={async () => { if (revoke) { await act(() => fieldApi.revokeKiosk(revoke.id), 'Kiosk revoked.'); setRevoke(null); } }} />
    </Stack>
  );
}
