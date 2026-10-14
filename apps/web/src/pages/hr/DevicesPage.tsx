import { ActionIcon, Alert, Button, Code, Drawer, Group, Menu, Modal, Paper, Select, SimpleGrid, Stack, Tabs, Text, TextInput } from '@mantine/core';
import { IconBan, IconCheck, IconDots, IconInfoCircle, IconKey, IconLink, IconRefresh } from '@tabler/icons-react';
import { useState } from 'react';
import { devices as devApi } from '../../api';
import { ApiError } from '../../api/client';
import type { Device, DeviceMapping, UnmappedPunch } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { Field } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge, toneColor } from '../../components/StatusBadge';
import { fmtAd, fmtTime, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

/** Active devices are shown as online/offline; pending and disabled keep their lifecycle status. */
function liveStatus(d: Device): string {
  if (d.status !== 'active') return d.status;
  return d.offline ? 'offline' : 'online';
}

const VENDOR: Record<string, string> = { zkteco: 'ZKTeco', hikvision: 'Hikvision', suprema: 'Suprema' };
const PROTOCOL: Record<string, string> = { adms: 'ADMS push', isapi: 'ISAPI' };

export default function DevicesPage() {
  const q = useQuery(() => devApi.list(), []);
  const unmapped = useQuery(() => devApi.unmapped(), []);
  const [sel, setSel] = useState<Device | null>(null);
  const mappings = useQuery(() => devApi.mappings(sel!.id), [sel?.id], { enabled: !!sel });
  const commands = useQuery(() => devApi.commands(sel!.id), [sel?.id], { enabled: !!sel });
  const [map, setMap] = useState<{ deviceId: string; pin: string; replace?: boolean; conflict?: string } | null>(null);
  const [mapEmp, setMapEmp] = useState<string | null>(null); const [mapFrom, setMapFrom] = useState<string | null>(todayNpt()); const [busy, setBusy] = useState(false);
  const [activate, setActivate] = useState<Device | null>(null); const [actName, setActName] = useState('');
  const [disable, setDisable] = useState<Device | null>(null);
  const [issued, setIssued] = useState<{ device: Device; key: string } | null>(null);
  const issueKey = async (d: Device) => { try { setIssued({ device: d, key: (await devApi.issueKey(d.id)).key }); } catch (e) { toast.err(e); } };
  const physical = (q.data?.devices ?? []).filter((d) => !['mobile', 'kiosk', 'manual'].includes(d.vendor));
  const unmappedRows = unmapped.data?.punches ?? [];

  const doMap = async () => {
    if (!map || !mapEmp || !mapFrom) return; setBusy(true);
    try { const r = await devApi.map(map.deviceId, { pin: map.pin, employeeId: mapEmp, from: mapFrom, ...(map.replace ? { replace: true } : {}) }); toast.ok(`PIN ${map.pin} mapped. ${r.reattributed} earlier day(s) re-attributed and recalculated.`); setMap(null); await Promise.all([unmapped.reload(), mappings.reload(), q.reload()]); }
    catch (e) {
      // The PIN already belongs to someone else from that date: ask before moving it (and their punches) across.
      if (e instanceof ApiError && e.status === 409 && !map.replace) setMap({ ...map, replace: true, conflict: e.message });
      else toast.err(e);
    } finally { setBusy(false); }
  };
  const syncUsers = async (d: Device) => {
    try { const r = await devApi.syncUsers(d.id); toast.ok(`${r.queued} user records queued for the terminal.`); if (sel?.id === d.id) await commands.reload(); } catch (e) { toast.err(e); }
  };
  const openActivate = (d: Device) => { setActivate(d); setActName(d.name ?? ''); };

  return (
    <Stack gap="lg">
      <PageHeader title="Devices" subtitle="Biometric terminals connected to the gateway. New terminals appear as pending and only send punches after you activate them." />
      <Tabs defaultValue="devices">
        <Tabs.List>
          <Tabs.Tab value="devices">Terminals <Text span c="dimmed" size="sm" className="num">{physical.length}</Text></Tabs.Tab>
          <Tabs.Tab value="unmapped">Unmapped punches <Text span c="dimmed" size="sm" className="num">{unmappedRows.length}</Text></Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="devices" pt="md">
          <DataTable<Device> rows={physical} rowKey={(d) => d.id} loading={q.loading} error={q.error}
            emptyTitle="No terminals have contacted the gateway yet"
            emptyHint="Point a ZKTeco/eSSL or Hikvision terminal at the gateway (see docs/gateway.md). It shows up here within a minute."
            onRowClick={setSel}
            columns={[
              {
                key: 'name', header: 'Device', render: (d) => (
                  <Group gap={10} wrap="nowrap">
                    <span className="hj-dot" style={{ background: toneColor(liveStatus(d)) }} aria-hidden />
                    <div style={{ minWidth: 0 }}>
                      <Text size="sm" fw={500} truncate>{d.name ?? 'Unnamed terminal'}</Text>
                      <Text size="xs" c="dimmed" ff="monospace">{d.serial_no}</Text>
                    </div>
                  </Group>
                ),
              },
              { key: 'vendor', header: 'Vendor', render: (d) => <><Text size="sm">{VENDOR[d.vendor] ?? d.vendor}</Text><Text size="xs" c="dimmed">{PROTOCOL[d.protocol] ?? d.protocol}</Text></> },
              { key: 'status', header: 'Status', render: (d) => <StatusBadge status={liveStatus(d)} /> },
              { key: 'seen', header: 'Last seen', render: (d) => <Text size="sm" c={d.last_seen_at ? 'inherit' : 'dimmed'}>{d.last_seen_at ? relativeTime(d.last_seen_at) : 'Never'}</Text> },
              { key: 'users', header: 'Mapped PINs', render: (d) => <span className="num">{d.mapped_users}</span>, align: 'right' },
              { key: 'fw', header: 'Firmware', render: (d) => <Text size="sm" c="dimmed">{d.fw_version ?? d.push_version ?? '—'}</Text> },
              {
                key: 'a', header: '', align: 'right', width: 90, render: (d) => (
                  <Group gap={4} justify="flex-end" wrap="nowrap" onClick={(e) => e.stopPropagation()}>
                    {d.status === 'pending' ? <Button size="xs" leftSection={<IconCheck {...ICON} />} onClick={() => openActivate(d)}>Activate</Button> : null}
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label="Device actions"><IconDots {...ICON} /></ActionIcon></Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Item leftSection={<IconInfoCircle {...ICON} />} onClick={() => setSel(d)}>View details</Menu.Item>
                        {d.status === 'pending' ? <Menu.Item leftSection={<IconCheck {...ICON} />} onClick={() => openActivate(d)}>Activate</Menu.Item> : null}
                        <Menu.Item leftSection={<IconLink {...ICON} />} onClick={() => setMap({ deviceId: d.id, pin: '' })}>Map a PIN</Menu.Item>
                        <Menu.Item leftSection={<IconRefresh {...ICON} />} onClick={() => void syncUsers(d)}>Sync users to device</Menu.Item>
                        {d.protocol !== 'adms' && d.company_id ? <Menu.Item leftSection={<IconKey {...ICON} />} onClick={() => void issueKey(d)}>Issue new device key</Menu.Item> : null}
                        {d.status === 'active' ? <><Menu.Divider /><Menu.Item color="red" leftSection={<IconBan {...ICON} />} onClick={() => setDisable(d)}>Disable device</Menu.Item></> : null}
                      </Menu.Dropdown>
                    </Menu>
                  </Group>
                ),
              },
            ]} />
        </Tabs.Panel>

        <Tabs.Panel value="unmapped" pt="md">
          <Text size="sm" c="dimmed" mb="sm">Punches from device PINs that are not linked to an employee. Map the PIN and earlier punches are re-attributed automatically.</Text>
          <DataTable<UnmappedPunch> rows={unmappedRows} rowKey={(p) => p.id} loading={unmapped.loading} error={unmapped.error}
            emptyTitle="Every punch is attributed"
            emptyHint="Punches from PINs that are not mapped to an employee will appear here."
            columns={[
              { key: 'pin', header: 'PIN', render: (p) => <Text size="sm" fw={500} ff="monospace">{p.device_user_pin}</Text> },
              { key: 'sn', header: 'Device', render: (p) => <Text size="sm" ff="monospace" c="dimmed">{p.device_sn}</Text> },
              { key: 'when', header: 'Last punch', render: (p) => <><Text size="sm">{fmtAd(p.local_date)}</Text><Text size="xs" c="dimmed" className="num">{fmtTime(p.punched_at)}</Text></> },
              { key: 'n', header: 'Punches for PIN', render: (p) => <span className="num">{p.total_for_pin}</span>, align: 'right' },
              { key: 'a', header: '', align: 'right', render: (p) => <Button size="xs" variant="default" leftSection={<IconLink {...ICON} />} onClick={() => { setMap({ deviceId: p.device_id, pin: p.device_user_pin }); setMapFrom(p.local_date); }}>Map PIN</Button> },
            ]} />
        </Tabs.Panel>
      </Tabs>

      <Drawer opened={!!sel} onClose={() => setSel(null)} position="right" size="lg" title={<Text fw={600}>{sel?.name ?? sel?.serial_no ?? ''}</Text>}>
        {sel ? (
          <Stack gap="lg">
            <SimpleGrid cols={2} spacing="md">
              <Field label="Status"><StatusBadge status={liveStatus(sel)} /></Field>
              <Field label="Last seen">{sel.last_seen_at ? relativeTime(sel.last_seen_at) : 'Never'}</Field>
              <Field label="Serial number">{sel.serial_no}</Field>
              <Field label="Vendor">{VENDOR[sel.vendor] ?? sel.vendor} ({PROTOCOL[sel.protocol] ?? sel.protocol})</Field>
              <Field label="Firmware">{sel.fw_version ?? sel.push_version ?? '—'}</Field>
              <Field label="Mapped PINs">{sel.mapped_users}</Field>
            </SimpleGrid>
            <Group gap="xs" wrap="wrap">
              <Button size="xs" leftSection={<IconLink {...ICON} />} onClick={() => setMap({ deviceId: sel.id, pin: '' })}>Map a PIN</Button>
              <Button size="xs" variant="default" leftSection={<IconRefresh {...ICON} />} onClick={() => void syncUsers(sel)}>Sync users</Button>
              <Select size="xs" w={190} placeholder="Send a command" value={null} data={['CHECK', 'INFO', 'LOG', 'REBOOT', 'DATA QUERY USERINFO']}
                onChange={async (v) => { if (!v) return; try { await devApi.command(sel.id, v); toast.ok(`${v} queued.`); await commands.reload(); } catch (e) { toast.err(e); } }} />
            </Group>
            <div>
              <SectionTitle hint="Which device PIN belongs to which employee.">PIN mappings</SectionTitle>
              <DataTable<DeviceMapping> rows={mappings.data?.mappings ?? []} rowKey={(m) => `${m.device_user_pin}-${m.valid}`} loading={mappings.loading} error={mappings.error}
                emptyTitle="No PIN mappings yet" emptyHint="Map a PIN to link punches from this terminal to an employee." maxHeight={260}
                columns={[
                  { key: 'pin', header: 'PIN', render: (m) => <Text size="sm" ff="monospace">{m.device_user_pin}</Text> },
                  { key: 'emp', header: 'Employee', render: (m) => <><Text size="sm" fw={500}>{m.name_en}</Text><Text size="xs" c="dimmed">{m.emp_code}</Text></> },
                  { key: 'valid', header: 'Valid', render: (m) => <Text size="sm" c="dimmed">{m.valid}</Text> },
                ]} />
            </div>
            <div>
              <SectionTitle>Recent commands</SectionTitle>
              <Paper withBorder p="sm" mah={200} style={{ overflow: 'auto' }}>
                {(commands.data?.commands ?? []).slice(0, 10).map((c) => (
                  <Group key={c.id} justify="space-between" wrap="nowrap" gap="sm" py={3}>
                    <Text size="xs" ff="monospace" truncate>#{c.id} {c.command.slice(0, 50)}</Text>
                    <Text size="xs" c="dimmed" style={{ whiteSpace: 'nowrap' }}>{c.status}{c.return_code !== null ? ` (${c.return_code})` : ''}</Text>
                  </Group>
                ))}
                {!commands.data?.commands.length ? <Text size="sm" c="dimmed">No commands sent yet.</Text> : null}
              </Paper>
            </div>
          </Stack>
        ) : null}
      </Drawer>

      <Modal opened={!!map} onClose={() => setMap(null)} title="Map a device PIN" centered>
        <Stack>
          <TextInput label="Device PIN" description="The user ID enrolled on the terminal." value={map?.pin ?? ''} onChange={(e) => map && setMap({ deviceId: map.deviceId, pin: e.currentTarget.value })} required />
          <EmployeePicker value={mapEmp} onChange={setMapEmp} required />
          <DateField label="Valid from" value={mapFrom} onChange={setMapFrom} required description="Unmapped punches from this date on are re-attributed to the employee." />
          {map?.replace ? <Alert variant="light" color="orange" title="This PIN is already in use">{map.conflict} Replacing it moves that person's punches from the chosen date to the employee above and recalculates both.</Alert> : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setMap(null)}>Cancel</Button>
            <Button loading={busy} color={map?.replace ? 'orange' : 'brand'} disabled={!map?.pin || !mapEmp || !mapFrom} onClick={doMap}>{map?.replace ? 'Replace mapping' : 'Map PIN'}</Button>
          </Group>
        </Stack>
      </Modal>

      <Modal opened={!!issued} onClose={() => setIssued(null)} title={`Device key for ${issued?.device.name ?? issued?.device.serial_no ?? ''}`} centered>
        <Stack>
          <Text size="sm" c="dimmed">Enter this key in the terminal's listening-host settings as <Code>?key=…</Code>. It is shown only once; issuing a new key stops the old one working.</Text>
          <Code block>{issued?.key}</Code>
          <Alert variant="light" color="yellow">Copy it now. Hajiri stores only a fingerprint of the key.</Alert>
          <Group justify="flex-end"><Button variant="default" onClick={() => void navigator.clipboard?.writeText(issued?.key ?? '').then(() => toast.ok('Key copied.'))}>Copy key</Button><Button onClick={() => setIssued(null)}>Done</Button></Group>
        </Stack>
      </Modal>
      <Modal opened={!!activate} onClose={() => setActivate(null)} title={`Activate ${activate?.serial_no ?? ''}`} centered>
        <Stack>
          <Text size="sm" c="dimmed">Only activate terminals you installed. Once active, this device can submit punches for your company.</Text>
          <TextInput label="Name" description="Where the terminal is, so people recognise it." value={actName} onChange={(e) => setActName(e.currentTarget.value)} placeholder="Front door" />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setActivate(null)}>Cancel</Button>
            <Button leftSection={<IconCheck {...ICON} />} onClick={async () => { if (!activate) return; try { await devApi.activate(activate.id, actName ? { name: actName } : {}); toast.ok('Device activated.'); setActivate(null); await q.reload(); } catch (e) { toast.err(e); } }}>Activate device</Button>
          </Group>
        </Stack>
      </Modal>

      <ConfirmDialog opened={!!disable} onClose={() => setDisable(null)} title={`Disable ${disable?.name ?? disable?.serial_no ?? 'device'}?`} confirmLabel="Disable device"
        message="Punches from this terminal will be rejected until it is activated again. Existing attendance is kept."
        onConfirm={async () => { if (!disable) return; try { await devApi.disable(disable.id); toast.ok('Device disabled.'); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
