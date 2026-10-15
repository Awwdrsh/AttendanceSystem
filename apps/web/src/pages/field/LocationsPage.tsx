import { ActionIcon, Anchor, Badge, Button, Group, Menu, Modal, MultiSelect, NumberInput, Paper, SegmentedControl, Select, SimpleGrid, Stack, Switch, Text, TextInput, Tooltip } from '@mantine/core';
import { IconCurrentLocation, IconDots, IconMapPin, IconPencil, IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { settings as settingsApi } from '../../api';
import { BREAK_LABEL, currentPosition, field as fieldApi, osmLink, type BreakKind, type FieldLocation, type FieldPolicy, type StaffRow } from '../../api/field';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const KINDS: BreakKind[] = ['lunch', 'tea', 'prayer', 'personal'];
const MODE_HINT = {
  off: 'Locations are not checked. Anyone can clock in from the browser anywhere.',
  flag: 'Clocking in away from a location needs a short reason and is flagged for the manager.',
  block: 'Clocking in away from a location is refused. Field staff and people working from home are not affected.',
};

interface LocForm { id?: string; name: string; address: string; branchId: string | null; lat: number | ''; lng: number | ''; radiusM: number | ''; active: boolean }
const emptyLoc: LocForm = { name: '', address: '', branchId: null, lat: '', lng: '', radiusM: 150, active: true };

function RulesCard() {
  const q = useQuery(() => fieldApi.policy(), []);
  const [p, setP] = useState<FieldPolicy | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (q.data) setP(q.data.policy); }, [q.data]);
  if (!p) return null;
  const setBreak = (k: BreakKind, patch: Partial<FieldPolicy['breaks'][BreakKind]>) => setP({ ...p, breaks: { ...p.breaks, [k]: { ...p.breaks[k], ...patch } } });
  const save = async () => { setBusy(true); try { setP((await fieldApi.savePolicy(p)).policy); toast.ok('Rules saved.'); } catch (e) { toast.err(e); } finally { setBusy(false); } };
  return (
    <Paper withBorder p="lg">
      <Stack gap="md">
        <div>
          <Text fw={600} mb={6}>Clocking in away from a location</Text>
          <SegmentedControl value={p.geofenceMode} onChange={(v) => setP({ ...p, geofenceMode: v as FieldPolicy['geofenceMode'] })}
            data={[{ value: 'off', label: 'Don\'t check' }, { value: 'flag', label: 'Ask for a reason' }, { value: 'block', label: 'Refuse' }]} />
          <Text size="sm" c="dimmed" mt={6}>{MODE_HINT[p.geofenceMode]}</Text>
        </div>
        <div>
          <Text fw={600} mb={6}>Breaks</Text>
          <Stack gap={6}>
            {KINDS.map((k) => (
              <Group key={k} gap="md" wrap="wrap">
                <Switch w={130} label={BREAK_LABEL[k]} checked={p.breaks[k].enabled} onChange={(e) => setBreak(k, { enabled: e.currentTarget.checked })} />
                <Switch label="Paid" disabled={!p.breaks[k].enabled} checked={p.breaks[k].paid} onChange={(e) => setBreak(k, { paid: e.currentTarget.checked })} />
                <NumberInput w={140} size="xs" disabled={!p.breaks[k].enabled} min={5} max={180} suffix=" min" value={p.breaks[k].maxMin} onChange={(v) => setBreak(k, { maxMin: Number(v) || 5 })} aria-label={`${BREAK_LABEL[k]} limit`} />
              </Group>
            ))}
          </Stack>
          <Text size="xs" c="dimmed" mt={6}>A break that runs past its limit is flagged and the line manager is told.</Text>
        </div>
        <div>
          <Text fw={600} mb={6}>Field visits</Text>
          <Stack gap={6}>
            <Switch label="The first visit of the day clocks the person in" checked={p.fieldVisitClockIn} onChange={(e) => setP({ ...p, fieldVisitClockIn: e.currentTarget.checked })} />
            <Switch label="Require a photo at the start of each visit" checked={p.fieldPhotoRequired} onChange={(e) => setP({ ...p, fieldPhotoRequired: e.currentTarget.checked })} />
          </Stack>
        </div>
        <Group justify="flex-end"><Button loading={busy} onClick={save}>Save rules</Button></Group>
      </Stack>
    </Paper>
  );
}

/** Settings, Locations: work locations (geofences), the rules for clocking in away from them, breaks and field staff. */
export default function LocationsPage() {
  const locs = useQuery(() => fieldApi.locations(), []);
  const staff = useQuery(() => fieldApi.staff(), []);
  const branches = useQuery(() => settingsApi.branches(), []);
  const [form, setForm] = useState<LocForm | null>(null);
  const [del, setDel] = useState<FieldLocation | null>(null);
  const [busy, setBusy] = useState(false);
  const [locating, setLocating] = useState(false);
  const [filter, setFilter] = useState<'field' | 'all'>('field');
  const locations = locs.data?.locations ?? [];
  const branchOpts = (branches.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }));
  const locOpts = locations.map((l) => ({ value: l.id, label: l.name }));

  const save = async () => {
    if (!form || form.lat === '' || form.lng === '') return;
    setBusy(true);
    try {
      const body = { name: form.name.trim(), address: form.address.trim() || null, branchId: form.branchId, lat: Number(form.lat), lng: Number(form.lng), radiusM: Number(form.radiusM) || 150, active: form.active };
      if (form.id) await fieldApi.updateLocation(form.id, body); else await fieldApi.createLocation(body);
      toast.ok(form.id ? 'Location saved.' : 'Location added.');
      setForm(null); await locs.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const here = async () => {
    setLocating(true);
    const p = await currentPosition(12_000);
    setLocating(false);
    if (p.lat === undefined || !form) { toast.info('Your location is not available. Allow location access, or type the coordinates.'); return; }
    setForm({ ...form, lat: Number(p.lat.toFixed(6)), lng: Number(p.lng!.toFixed(6)) });
    if ((p.accuracyM ?? 0) > 100) toast.info(`Location accuracy is about ${p.accuracyM} m. Stand outside or near a window for a better fix.`);
  };
  const updateStaff = async (s: StaffRow, b: { fieldWork?: boolean; locationIds?: string[] }) => {
    try { await fieldApi.updateStaff(s.id, b); staff.setData({ staff: (staff.data?.staff ?? []).map((x) => (x.id === s.id ? { ...x, ...(b.fieldWork !== undefined ? { field_work: b.fieldWork } : {}), ...(b.locationIds ? { location_ids: b.locationIds } : {}) } : x)) }); }
    catch (e) { toast.err(e); }
  };
  const resetPin = async (s: StaffRow) => {
    try { await fieldApi.resetKioskPin(s.id); toast.ok(`Kiosk PIN reset for ${s.name_en}. They can set a new one in the app.`); await staff.reload(); } catch (e) { toast.err(e); }
  };
  const people = (staff.data?.staff ?? []).filter((s) => filter === 'all' || s.field_work || s.location_ids.length > 0);

  return (
    <Stack gap="xl">
      <div>
        <SectionTitle hint="Places people clock in from the browser or phone. A punch within the radius counts as at work."
          actions={<Button size="sm" leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setForm({ ...emptyLoc })}>Add location</Button>}>Work locations</SectionTitle>
        <DataTable rows={locations} rowKey={(l) => l.id} loading={locs.loading} error={locs.error} emptyTitle="No locations yet"
          emptyHint="Add your office so clock-ins can be checked against it. Use the button while standing in the office to fill in the coordinates."
          columns={[
            { key: 'name', header: 'Name', render: (l) => <div><Group gap={6}><Text size="sm" fw={500}>{l.name}</Text>{l.active ? null : <Badge size="xs" color="gray" variant="light">Inactive</Badge>}</Group>{l.address ? <Text size="xs" c="dimmed">{l.address}</Text> : null}</div> },
            { key: 'branch', header: 'Branch', render: (l) => l.branch_name ?? <Text size="sm" c="dimmed">All branches</Text> },
            { key: 'radius', header: 'Radius', align: 'right', render: (l) => <span className="num">{l.radius_m} m</span> },
            { key: 'people', header: 'Tied to', align: 'right', render: (l) => <span className="num">{l.employee_ids.length ? `${l.employee_ids.length} people` : '—'}</span> },
            { key: 'map', header: 'Map', render: (l) => <Anchor size="sm" href={osmLink(l.lat, l.lng)} target="_blank" rel="noopener noreferrer">Open map</Anchor> },
            { key: 'act', header: '', align: 'right', render: (l) => <Group gap={4} justify="flex-end" wrap="nowrap">
              <Tooltip label="Edit"><ActionIcon variant="subtle" color="gray" onClick={() => setForm({ id: l.id, name: l.name, address: l.address ?? '', branchId: l.branch_id, lat: l.lat, lng: l.lng, radiusM: l.radius_m, active: l.active })} aria-label="Edit"><IconPencil size={16} stroke={1.6} /></ActionIcon></Tooltip>
              <Tooltip label="Delete"><ActionIcon variant="subtle" color="red" onClick={() => setDel(l)} aria-label="Delete"><IconTrash size={16} stroke={1.6} /></ActionIcon></Tooltip>
            </Group> },
          ]} />
      </div>

      <div>
        <SectionTitle hint="How strictly locations are checked, which breaks people can take, and how field visits work.">Rules</SectionTitle>
        <RulesCard />
      </div>

      <div>
        <SectionTitle hint="Field staff record client and site visits. Tying someone to locations replaces their branch's locations for them."
          actions={<SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as 'field' | 'all')} data={[{ value: 'field', label: 'Field staff' }, { value: 'all', label: 'Everyone' }]} />}>Staff</SectionTitle>
        <DataTable rows={people} rowKey={(s) => s.id} loading={staff.loading} error={staff.error}
          emptyTitle={filter === 'field' ? 'No field staff yet' : 'No employees'} emptyHint="Switch to Everyone and turn on field visits for people who work at client sites."
          columns={[
            { key: 'name', header: 'Name', sticky: true, render: (s) => <div><Text size="sm" fw={500}>{s.name_en}</Text><Text size="xs" c="dimmed">{s.emp_code}{s.designation ? ` · ${s.designation}` : ''}</Text></div> },
            { key: 'field', header: 'Field visits', render: (s) => <Switch checked={s.field_work} onChange={(e) => void updateStaff(s, { fieldWork: e.currentTarget.checked })} aria-label={`Field visits for ${s.name_en}`} /> },
            { key: 'locs', header: 'Locations', render: (s) => <MultiSelect size="xs" w={240} data={locOpts} value={s.location_ids} placeholder={s.location_ids.length ? undefined : s.branch_name ? `${s.branch_name} locations` : 'Branch locations'}
              onChange={(v) => void updateStaff(s, { locationIds: v })} clearable searchable aria-label={`Locations for ${s.name_en}`} /> },
            { key: 'pin', header: 'Kiosk PIN', render: (s) => s.kiosk_pin_locked ? <Badge color="red" variant="light">Locked</Badge> : s.kiosk_pin_set ? <Badge variant="light">Set</Badge> : <Text size="sm" c="dimmed">Not set</Text> },
            { key: 'act', header: '', align: 'right', render: (s) => <Menu position="bottom-end">
              <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label="More"><IconDots size={16} /></ActionIcon></Menu.Target>
              <Menu.Dropdown><Menu.Item onClick={() => void resetPin(s)}>Reset kiosk PIN and QR badge</Menu.Item></Menu.Dropdown>
            </Menu> },
          ]} />
      </div>

      <Modal opened={!!form} onClose={() => setForm(null)} title={form?.id ? 'Edit location' : 'Add location'} centered size="lg">
        {form ? (
          <Stack>
            <TextInput label="Name" placeholder="Head office" value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} required maxLength={80} />
            <TextInput label="Address" placeholder="Street, city" value={form.address} onChange={(e) => setForm({ ...form, address: e.currentTarget.value })} maxLength={300} />
            <Select label="Branch" description="Leave empty to use it for every branch." data={branchOpts} value={form.branchId} onChange={(v) => setForm({ ...form, branchId: v })} clearable placeholder="All branches" />
            <SimpleGrid cols={{ base: 1, sm: 3 }}>
              <NumberInput label="Latitude" decimalScale={6} min={-90} max={90} value={form.lat} onChange={(v) => setForm({ ...form, lat: v === '' ? '' : Number(v) })} required />
              <NumberInput label="Longitude" decimalScale={6} min={-180} max={180} value={form.lng} onChange={(v) => setForm({ ...form, lng: v === '' ? '' : Number(v) })} required />
              <NumberInput label="Radius" suffix=" m" min={20} max={5000} value={form.radiusM} onChange={(v) => setForm({ ...form, radiusM: v === '' ? '' : Number(v) })} />
            </SimpleGrid>
            <Group justify="space-between" wrap="wrap">
              <Button variant="default" size="sm" loading={locating} leftSection={<IconCurrentLocation size={16} stroke={1.6} />} onClick={here}>Use my current location</Button>
              {form.lat !== '' && form.lng !== '' ? <Anchor size="sm" href={osmLink(Number(form.lat), Number(form.lng))} target="_blank" rel="noopener noreferrer"><Group gap={4}><IconMapPin size={14} />Check on the map</Group></Anchor> : null}
            </Group>
            <Switch label="Active" checked={form.active} onChange={(e) => setForm({ ...form, active: e.currentTarget.checked })} />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setForm(null)}>Cancel</Button>
              <Button loading={busy} disabled={!form.name.trim() || form.lat === '' || form.lng === ''} onClick={save}>Save</Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>
      <ConfirmDialog opened={!!del} title="Delete location" message={del ? `Delete ${del.name}? Clock-ins will no longer be checked against it. Past punches keep their record.` : ''}
        confirmLabel="Delete" onClose={() => setDel(null)} onConfirm={async () => {
          if (!del) return;
          try { await fieldApi.deleteLocation(del.id); toast.ok('Location deleted.'); setDel(null); await locs.reload(); } catch (e) { toast.err(e); }
        }} />
    </Stack>
  );
}
