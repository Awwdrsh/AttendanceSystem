import { ActionIcon, Button, Group, Modal, Select, Stack, Text, TextInput, Tooltip } from '@mantine/core';
import { IconTrash, IconUserShare } from '@tabler/icons-react';
import { useState } from 'react';
import { get } from '../api/client';
import { flows } from '../api/flows';
import { useAuth } from '../auth/AuthProvider';
import { fmtDate, todayNpt } from '../lib/format';
import { toast } from '../lib/toast';
import { useQuery } from '../lib/useQuery';
import { DateField } from './BsDatePicker';

/**
 * "Away? Hand over your approvals": while a delegation is active the chosen colleague receives and decides the
 * requests that would come to you. While you are on leave you are skipped and only the delegate gets them.
 */
export function DelegateApprovals() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const list = useQuery(() => flows.delegates(), []);
  const people = useQuery(() => (open ? get<{ users: Array<{ id: string; name: string }> }>('/workflow/delegates/candidates') : Promise.resolve({ users: [] })), [open]);
  const [delegateId, setDelegateId] = useState<string | null>(null);
  const [from, setFrom] = useState<string | null>(todayNpt());
  const [to, setTo] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const mine = (list.data?.delegates ?? []).filter((d) => d.user_id === user?.id);
  const active = mine.find((d) => d.from_date <= todayNpt() && d.to_date >= todayNpt());

  const save = async () => {
    if (!delegateId || !from || !to) return;
    setBusy(true);
    try {
      await flows.addDelegate({ delegateId, from, to, ...(note.trim() ? { note: note.trim() } : {}) });
      toast.ok('Your approvals will go to your delegate on those days.');
      setDelegateId(null); setTo(null); setNote('');
      await list.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  return (
    <>
      <Button variant="default" leftSection={<IconUserShare size={16} stroke={1.6} />} onClick={() => setOpen(true)}>
        {active ? `Delegated to ${active.delegate_name}` : 'Delegate approvals'}
      </Button>
      <Modal opened={open} onClose={() => setOpen(false)} title="Hand over your approvals" size="md">
        <Stack gap="sm">
          <Text size="sm" c="dimmed">While a delegation is on, the person you choose also receives and decides requests that would come to you. When you are on leave, only they get them.</Text>
          {mine.length ? (
            <Stack gap={6}>
              {mine.map((d) => (
                <Group key={d.id} justify="space-between" wrap="nowrap">
                  <Text size="sm">{d.delegate_name}, {fmtDate(d.from_date)} to {fmtDate(d.to_date)}{d.note ? ` · ${d.note}` : ''}</Text>
                  <Tooltip label="Cancel this delegation">
                    <ActionIcon variant="subtle" color="red" aria-label="Cancel delegation" onClick={async () => { try { await flows.cancelDelegate(d.id); await list.reload(); } catch (e) { toast.err(e); } }}>
                      <IconTrash size={16} stroke={1.6} />
                    </ActionIcon>
                  </Tooltip>
                </Group>
              ))}
            </Stack>
          ) : null}
          <Select label="Delegate" placeholder="Choose a colleague" searchable data={(people.data?.users ?? []).map((u) => ({ value: u.id, label: u.name }))} value={delegateId} onChange={setDelegateId} />
          <Group grow>
            <DateField label="From" value={from} onChange={setFrom} minDate={todayNpt()} />
            <DateField label="To" value={to} onChange={setTo} minDate={from ?? todayNpt()} />
          </Group>
          <TextInput label="Note (optional)" placeholder="e.g. Annual leave" value={note} onChange={(e) => setNote(e.currentTarget.value)} maxLength={300} />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpen(false)}>Close</Button>
            <Button loading={busy} disabled={!delegateId || !from || !to} onClick={save}>Delegate</Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
