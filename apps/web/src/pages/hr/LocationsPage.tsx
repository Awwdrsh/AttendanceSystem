import { ActionIcon, Button, Group, Modal, NumberInput, SimpleGrid, Stack, Text, TextInput, Tooltip } from '@mantine/core';
import { IconCurrentLocation, IconMapPin, IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Location } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

export default function LocationsPage() {
  const q = useQuery(() => shiftApi.locations(), []);
  const [edit, setEdit] = useState<{ id?: string; name: string; lat: number | string; lng: number | string; radiusM: number | string } | null>(null);
  const [del, setDel] = useState<Location | null>(null); const [busy, setBusy] = useState(false);
  const useHere = () => navigator.geolocation?.getCurrentPosition((p) => setEdit((e) => e && ({ ...e, lat: Number(p.coords.latitude.toFixed(6)), lng: Number(p.coords.longitude.toFixed(6)) })), () => toast.info('Your browser did not share a location. Allow location access and try again, or enter the coordinates.'));
  const save = async () => {
    if (!edit) return; setBusy(true);
    try { const { id, ...b } = edit; const body = { name: b.name, lat: Number(b.lat), lng: Number(b.lng), radiusM: Number(b.radiusM) }; if (id) await shiftApi.updateLocation(id, body); else await shiftApi.createLocation(body); toast.ok('Location saved.'); setEdit(null); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="lg">
      <PageHeader title="Locations" subtitle="Geofences for phone and web clock-ins. Clock-ins outside a location are allowed but flagged and need a reason."
        actions={<Button leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ name: '', lat: 27.7172, lng: 85.324, radiusM: 150 })}>Add location</Button>} />
      <DataTable<Location> rows={q.data?.locations ?? []} rowKey={(l) => l.id} loading={q.loading} error={q.error}
        emptyTitle="No locations yet" emptyHint="Without a location, phone clock-ins are accepted without a geofence check."
        onRowClick={(l) => setEdit({ id: l.id, name: l.name, lat: Number(l.lat), lng: Number(l.lng), radiusM: l.radius_m })}
        columns={[
          { key: 'name', header: 'Location', render: (l) => <Group gap={8} wrap="nowrap"><IconMapPin {...ICON} color="var(--hj-muted)" /><Text size="sm" fw={500}>{l.name}</Text></Group> },
          { key: 'coords', header: 'Coordinates', render: (l) => <Text size="sm" c="dimmed" className="num">{Number(l.lat).toFixed(5)}, {Number(l.lng).toFixed(5)}</Text> },
          { key: 'radius_m', header: 'Radius', align: 'right', render: (l) => <span className="num">{l.radius_m} m</span> },
          {
            key: 'a', header: '', align: 'right', width: 56, render: (l) => (
              <Tooltip label="Delete location">
                <ActionIcon variant="subtle" color="gray" aria-label={`Delete ${l.name}`} onClick={(e) => { e.stopPropagation(); setDel(l); }}><IconTrash {...ICON} /></ActionIcon>
              </Tooltip>
            ),
          },
        ]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? 'Edit location' : 'New location'} centered>
        {edit ? (
          <Stack>
            <TextInput label="Name" placeholder="Head office" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.currentTarget.value })} required />
            <SimpleGrid cols={2}>
              <NumberInput label="Latitude" value={edit.lat} onChange={(v) => setEdit({ ...edit, lat: v })} decimalScale={6} />
              <NumberInput label="Longitude" value={edit.lng} onChange={(v) => setEdit({ ...edit, lng: v })} decimalScale={6} />
            </SimpleGrid>
            <NumberInput label="Radius" description="Clock-ins within this distance count as on site" suffix=" m" value={edit.radiusM} onChange={(v) => setEdit({ ...edit, radiusM: v })} min={20} max={5000} />
            <Text size="xs" c="dimmed">Tip: open this page on a phone at the entrance and use your current position.</Text>
            <Group justify="space-between">
              <Button variant="subtle" leftSection={<IconCurrentLocation {...ICON} />} onClick={useHere}>Use my position</Button>
              <Group gap="xs">
                <Button variant="default" onClick={() => setEdit(null)}>Cancel</Button>
                <Button loading={busy} disabled={!edit.name} onClick={save}>Save location</Button>
              </Group>
            </Group>
          </Stack>
        ) : null}
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name ?? 'location'}?`} confirmLabel="Delete location" message="Phone clock-ins will no longer be checked against this location." onConfirm={async () => { if (!del) return; try { await shiftApi.deleteLocation(del.id); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
