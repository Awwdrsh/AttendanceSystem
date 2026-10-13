import { Badge, Button, Group, Modal, Paper, Select, Stack, Tabs, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { devices as devApi } from '../../api';
import type { Device, DeviceMapping, UnmappedPunch } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { fmtTime, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function DevicesPage() {
  const { t, lang } = useI18n();
  const q = useQuery(() => devApi.list(), []);
  const unmapped = useQuery(() => devApi.unmapped(), []);
  const [sel, setSel] = useState<Device | null>(null);
  const mappings = useQuery(() => devApi.mappings(sel!.id), [sel?.id], { enabled: !!sel });
  const commands = useQuery(() => devApi.commands(sel!.id), [sel?.id], { enabled: !!sel });
  const [map, setMap] = useState<{ deviceId: string; pin: string } | null>(null);
  const [mapEmp, setMapEmp] = useState<string | null>(null); const [mapFrom, setMapFrom] = useState<string | null>(todayNpt()); const [busy, setBusy] = useState(false);
  const [activate, setActivate] = useState<Device | null>(null); const [actName, setActName] = useState('');
  const physical = (q.data?.devices ?? []).filter((d) => !['mobile', 'kiosk', 'manual'].includes(d.vendor));
  const doMap = async () => {
    if (!map || !mapEmp || !mapFrom) return; setBusy(true);
    try { const r = await devApi.map(map.deviceId, { pin: map.pin, employeeId: mapEmp, from: mapFrom }); toast.ok(`PIN ${map.pin} mapped. ${r.reattributed} earlier day(s) re-attributed and recomputed.`); setMap(null); await Promise.all([unmapped.reload(), mappings.reload(), q.reload()]); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('devices')} subtitle={lang === 'ne' ? 'नयाँ यन्त्र "पेन्डिङ" देखिन्छ; सक्रिय गरेपछि मात्र हाजिरी स्वीकार हुन्छ।' : 'New terminals appear as pending; punches are accepted only after activation.'} />
      <Tabs defaultValue="devices">
        <Tabs.List><Tabs.Tab value="devices">{t('devices')} ({physical.length})</Tabs.Tab><Tabs.Tab value="unmapped">{t('unmappedPunches')} ({unmapped.data?.punches.length ?? 0})</Tabs.Tab></Tabs.List>
        <Tabs.Panel value="devices" pt="md">
          <DataTable<Device> rows={physical} rowKey={(d) => d.id} loading={q.loading} error={q.error} emptyTitle="No terminals have contacted the gateway yet." emptyHint="Point a ZKTeco/eSSL or Hikvision terminal at the gateway (see docs/gateway.md). It will show up here within a minute." onRowClick={setSel}
            columns={[
              { key: 'sn', header: 'Serial', render: (d) => <><Text size="sm" fw={500}>{d.name ?? d.serial_no}</Text><Text size="xs" c="dimmed">{d.serial_no} · {d.vendor}/{d.protocol}</Text></> },
              { key: 'status', header: t('status'), render: (d) => <Group gap={4}><StatusBadge status={d.status} />{d.status === 'active' ? <Badge variant="light" color={d.offline ? 'red' : 'teal'}>{d.offline ? t('offlineDevice') : t('online')}</Badge> : null}</Group> },
              { key: 'seen', header: t('lastSeen'), render: (d) => relativeTime(d.last_seen_at) },
              { key: 'users', header: 'Mapped', render: (d) => d.mapped_users, align: 'right' },
              { key: 'fw', header: 'Firmware', render: (d) => d.fw_version ?? d.push_version ?? '—' },
              { key: 'a', header: '', render: (d) => d.status === 'pending' ? <Button size="xs" onClick={(e) => { e.stopPropagation(); setActivate(d); setActName(d.name ?? ''); }}>{t('activate')}</Button> : d.status === 'active' ? <Button size="xs" variant="subtle" color="red" onClick={async (e) => { e.stopPropagation(); try { await devApi.disable(d.id); await q.reload(); } catch (err) { toast.err(err); } }}>{t('disable')}</Button> : null },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="unmapped" pt="md">
          <DataTable<UnmappedPunch> rows={unmapped.data?.punches ?? []} rowKey={(p) => p.id} loading={unmapped.loading} emptyTitle="Every punch is attributed." emptyHint="Punches from PINs that are not mapped to an employee will appear here."
            columns={[
              { key: 'sn', header: 'Device', render: (p) => p.device_sn }, { key: 'pin', header: 'PIN', render: (p) => <strong>{p.device_user_pin}</strong> },
              { key: 'when', header: 'When', render: (p) => `${p.local_date} ${fmtTime(p.punched_at)}` }, { key: 'n', header: 'Punches for PIN', render: (p) => p.total_for_pin, align: 'right' },
              { key: 'a', header: '', render: (p) => <Button size="xs" onClick={() => { setMap({ deviceId: p.device_id, pin: p.device_user_pin }); setMapFrom(p.local_date); }}>{t('mapPin')}</Button> },
            ]} />
        </Tabs.Panel>
      </Tabs>

      <Modal opened={!!sel} onClose={() => setSel(null)} title={sel?.name ?? sel?.serial_no} size="lg" centered>
        {sel ? <Stack>
          <Group gap="xs"><StatusBadge status={sel.status} /><Text size="sm">{sel.serial_no} · {sel.vendor} · last seen {relativeTime(sel.last_seen_at)}</Text></Group>
          <Group gap="xs"><Button size="xs" variant="light" onClick={() => { setMap({ deviceId: sel.id, pin: '' }); }}>{t('mapPin')}</Button><Button size="xs" variant="light" onClick={async () => { try { const r = await devApi.syncUsers(sel.id); toast.ok(`${r.queued} user records queued for the terminal.`); await commands.reload(); } catch (e) { toast.err(e); } }}>Sync users to device</Button>
            <Select size="xs" placeholder="Send command" data={['CHECK', 'INFO', 'LOG', 'REBOOT', 'DATA QUERY USERINFO']} onChange={async (v) => { if (!v) return; try { await devApi.command(sel.id, v); toast.ok(`${v} queued.`); await commands.reload(); } catch (e) { toast.err(e); } }} /></Group>
          <Text fw={600} size="sm">Mappings</Text>
          <DataTable<DeviceMapping> rows={mappings.data?.mappings ?? []} rowKey={(m) => `${m.device_user_pin}-${m.valid}`} loading={mappings.loading} emptyTitle="No PIN mappings yet." maxHeight={240}
            columns={[{ key: 'pin', header: 'PIN', render: (m) => m.device_user_pin }, { key: 'emp', header: t('employees'), render: (m) => `${m.emp_code} · ${m.name_en}` }, { key: 'valid', header: 'Valid', render: (m) => m.valid }]} />
          <Text fw={600} size="sm">Recent commands</Text>
          <Paper withBorder p="xs" radius="md" mah={160} style={{ overflow: 'auto' }}>{(commands.data?.commands ?? []).slice(0, 10).map((c) => <Text key={c.id} size="xs" ff="monospace">#{c.id} {c.command.slice(0, 50)} · {c.status}{c.return_code !== null ? ` (${c.return_code})` : ''}</Text>)}{!commands.data?.commands.length ? <Text size="xs" c="dimmed">None.</Text> : null}</Paper>
        </Stack> : null}
      </Modal>
      <Modal opened={!!map} onClose={() => setMap(null)} title={t('mapPin')} centered>
        <Stack>
          <TextInput label="Device PIN" value={map?.pin ?? ''} onChange={(e) => map && setMap({ ...map, pin: e.currentTarget.value })} required />
          <EmployeePicker value={mapEmp} onChange={setMapEmp} required />
          <BsDatePicker label="Valid from" value={mapFrom} onChange={setMapFrom} required description="Earlier unmapped punches from this date on are re-attributed to the employee." />
          <Group justify="flex-end"><Button loading={busy} disabled={!map?.pin || !mapEmp || !mapFrom} onClick={doMap}>{t('save')}</Button></Group>
        </Stack>
      </Modal>
      <Modal opened={!!activate} onClose={() => setActivate(null)} title={`${t('activate')} ${activate?.serial_no ?? ''}`} centered>
        <Stack>
          <Text size="sm">Only activate terminals you installed. Activation lets this device submit punches for your company.</Text>
          <TextInput label="Name" value={actName} onChange={(e) => setActName(e.currentTarget.value)} placeholder="Front door" />
          <Group justify="flex-end"><Button onClick={async () => { if (!activate) return; try { await devApi.activate(activate.id, actName ? { name: actName } : {}); toast.ok('Device activated.'); setActivate(null); await q.reload(); } catch (e) { toast.err(e); } }}>{t('activate')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
