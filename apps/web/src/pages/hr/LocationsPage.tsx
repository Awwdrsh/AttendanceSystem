import { Button, Group, Modal, NumberInput, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Location } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function LocationsPage() {
  const { t } = useI18n();
  const q = useQuery(() => shiftApi.locations(), []);
  const [edit, setEdit] = useState<{ id?: string; name: string; lat: number | string; lng: number | string; radiusM: number | string } | null>(null);
  const [del, setDel] = useState<Location | null>(null); const [busy, setBusy] = useState(false);
  const useHere = () => navigator.geolocation?.getCurrentPosition((p) => setEdit((e) => e && ({ ...e, lat: Number(p.coords.latitude.toFixed(6)), lng: Number(p.coords.longitude.toFixed(6)) })), () => toast.info('Location not available in this browser.'));
  return (
    <Stack>
      <PageHeader title={t('locations')} subtitle="Geofences for mobile and web punches. Punches outside are allowed but flagged and need a reason (plan §13.4)." actions={<Button onClick={() => setEdit({ name: '', lat: 27.7172, lng: 85.324, radiusM: 150 })}>{t('create')}</Button>} />
      <DataTable<Location> rows={q.data?.locations ?? []} rowKey={(l) => l.id} loading={q.loading} error={q.error} emptyTitle="No locations yet." emptyHint="Without locations, mobile punches are accepted without a geofence check." onRowClick={(l) => setEdit({ id: l.id, name: l.name, lat: Number(l.lat), lng: Number(l.lng), radiusM: l.radius_m })}
        columns={[{ key: 'name', header: t('name') }, { key: 'lat', header: 'Latitude' }, { key: 'lng', header: 'Longitude' }, { key: 'radius_m', header: 'Radius (m)', align: 'right' },
          { key: 'a', header: '', render: (l) => <Button size="xs" variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); setDel(l); }}>{t('delete')}</Button> }]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? t('edit') : 'New location'} centered>
        {edit ? <Stack>
          <TextInput label={t('name')} value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.currentTarget.value })} required />
          <Group grow><NumberInput label="Latitude" value={edit.lat} onChange={(v) => setEdit({ ...edit, lat: v })} decimalScale={6} /><NumberInput label="Longitude" value={edit.lng} onChange={(v) => setEdit({ ...edit, lng: v })} decimalScale={6} /></Group>
          <NumberInput label="Radius (m)" value={edit.radiusM} onChange={(v) => setEdit({ ...edit, radiusM: v })} min={20} max={5000} />
          <Text size="xs" c="dimmed">Tip: open this page on a phone at the entrance and use "Use my position".</Text>
          <Group justify="space-between"><Button variant="light" onClick={useHere}>Use my position</Button><Button loading={busy} disabled={!edit.name} onClick={async () => { setBusy(true); try { const { id, ...b } = edit; const body = { name: b.name, lat: Number(b.lat), lng: Number(b.lng), radiusM: Number(b.radiusM) }; if (id) await shiftApi.updateLocation(id, body); else await shiftApi.createLocation(body); toast.ok('Location saved.'); setEdit(null); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name ?? ''}?`} confirmLabel={t('delete')} message="Mobile punches will no longer be checked against this location." onConfirm={async () => { if (!del) return; try { await shiftApi.deleteLocation(del.id); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
