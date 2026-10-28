import { Badge, Button, Group, Modal, SegmentedControl, Select, SimpleGrid, Stack, Text, TextInput, Textarea } from '@mantine/core';
import { IconPlus, IconSearch } from '@tabler/icons-react';
import { useState } from 'react';
import { lifecycle } from '../../api/lifecycle';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { SectionTitle } from '../../components/PageHeader';
import { fmtAd, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };
const CONDITIONS = [
  { value: 'new', label: 'New' },
  { value: 'good', label: 'Good' },
  { value: 'fair', label: 'Fair' },
  { value: 'poor', label: 'Poor' },
  { value: 'damaged', label: 'Damaged' },
  { value: 'lost', label: 'Lost' },
];
const STATUS_COLOR = { available: 'teal', assigned: 'blue', retired: 'gray' };

/** Company assets (laptops, phones, ID cards) and who holds them. Assets still held block an exit from completing. */
export function AssetsPanel() {
  const [status, setStatus] = useState('all');
  const [search, setSearch] = useState('');
  const q = useQuery(
    () => lifecycle.assets({ ...(status !== 'all' ? { status } : {}), ...(search.trim() ? { search: search.trim() } : {}) }),
    [status, search],
  );
  const types = useQuery(() => lifecycle.assetTypes(), []);
  const [create, setCreate] = useState(null);
  const [assign, setAssign] = useState(null);
  const [ret, setRet] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn, ok, close) => {
    setBusy(true);
    try {
      await fn();
      toast.ok(ok);
      close();
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="md">
      <SectionTitle
        hint="Hand equipment to people and record its return. Someone cannot finish their exit while they still hold an asset."
        actions={
          <Button
            size="sm"
            leftSection={<IconPlus {...ICON} />}
            onClick={() => setCreate({ tag: '', name: '', typeId: null, serial: '', condition: 'good', purchaseDate: null, cost: '' })}
          >
            Add asset
          </Button>
        }
      >
        Assets
      </SectionTitle>
      <Group gap="sm" wrap="wrap">
        <SegmentedControl
          value={status}
          onChange={setStatus}
          data={[
            { value: 'all', label: 'All' },
            { value: 'assigned', label: 'With someone' },
            { value: 'available', label: 'Available' },
            { value: 'retired', label: 'Retired' },
          ]}
        />
        <TextInput
          placeholder="Search tag, name or serial"
          leftSection={<IconSearch {...ICON} />}
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
          w={260}
        />
      </Group>
      <DataTable
        rows={q.data?.assets ?? []}
        rowKey={(a) => a.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No assets recorded"
        emptyHint="Add laptops, phones and ID cards to track who holds them."
        columns={[
          {
            key: 'tag',
            header: 'Asset',
            render: (a) => (
              <div>
                <Text size="sm" fw={500}>
                  {a.name}
                </Text>
                <Text size="xs" c="dimmed" ff="monospace">
                  {a.tag}
                  {a.serial ? `, ${a.serial}` : ''}
                </Text>
              </div>
            ),
          },
          { key: 'type', header: 'Type', render: (a) => a.type_name ?? '—' },
          {
            key: 'status',
            header: 'Status',
            render: (a) => (
              <Badge variant="light" color={STATUS_COLOR[a.status] ?? 'gray'}>
                {a.status === 'assigned' ? 'With someone' : a.status.charAt(0).toUpperCase() + a.status.slice(1)}
              </Badge>
            ),
          },
          {
            key: 'holder',
            header: 'Held by',
            render: (a) =>
              a.employee_name ? (
                <div>
                  <Text size="sm">{a.employee_name}</Text>
                  <Text size="xs" c="dimmed">
                    since {fmtAd(a.assigned_on)}
                  </Text>
                </div>
              ) : (
                <Text size="sm" c="dimmed">
                  —
                </Text>
              ),
          },
          {
            key: 'cond',
            header: 'Condition',
            render: (a) => (
              <Text size="sm" tt="capitalize">
                {a.condition}
              </Text>
            ),
          },
          { key: 'cost', header: 'Cost', align: 'right', render: (a) => <span className="num">{a.cost ? fmtMoney(a.cost) : '—'}</span> },
          {
            key: 'act',
            header: '',
            align: 'right',
            render: (a) =>
              a.status === 'assigned' ? (
                <Button size="xs" variant="default" onClick={() => setRet({ asset: a, condition: a.condition, on: todayNpt(), notes: '' })}>
                  Record return
                </Button>
              ) : a.status === 'available' ? (
                <Button size="xs" variant="default" onClick={() => setAssign({ asset: a, employeeId: null, on: todayNpt(), notes: '' })}>
                  Hand over
                </Button>
              ) : null,
          },
        ]}
      />

      <Modal opened={!!create} onClose={() => setCreate(null)} title="Add asset" centered size="lg">
        {create ? (
          <Stack>
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <TextInput
                label="Asset tag"
                placeholder="LT-001"
                value={create.tag ?? ''}
                onChange={(e) => setCreate({ ...create, tag: e.currentTarget.value })}
                required
              />
              <TextInput
                label="Name"
                placeholder="ThinkPad T14"
                value={create.name ?? ''}
                onChange={(e) => setCreate({ ...create, name: e.currentTarget.value })}
                required
              />
              <Select
                label="Type"
                value={create.typeId ?? null}
                onChange={(v) => setCreate({ ...create, typeId: v })}
                data={(types.data?.types ?? []).map((t) => ({ value: t.id, label: t.name }))}
                clearable
              />
              <TextInput
                label="Serial number"
                value={create.serial ?? ''}
                onChange={(e) => setCreate({ ...create, serial: e.currentTarget.value })}
              />
              <Select
                label="Condition"
                value={create.condition ?? 'good'}
                allowDeselect={false}
                onChange={(v) => v && setCreate({ ...create, condition: v })}
                data={CONDITIONS.filter((c) => c.value !== 'lost')}
              />
              <DateField
                label="Purchased"
                value={create.purchaseDate ?? null}
                onChange={(v) => setCreate({ ...create, purchaseDate: v })}
              />
              <TextInput
                label="Cost (NPR)"
                value={create.cost ?? ''}
                onChange={(e) => setCreate({ ...create, cost: e.currentTarget.value.replace(/[^\d.]/g, '') })}
              />
            </SimpleGrid>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setCreate(null)}>
                Cancel
              </Button>
              <Button
                loading={busy}
                disabled={!create.tag?.trim() || !create.name?.trim()}
                onClick={() =>
                  run(
                    () =>
                      lifecycle.createAsset({
                        tag: create.tag.trim(),
                        name: create.name.trim(),
                        typeId: create.typeId ?? null,
                        serial: create.serial?.trim() || null,
                        condition: create.condition ?? 'good',
                        purchaseDate: create.purchaseDate ?? null,
                        cost: create.cost ? Number(create.cost).toFixed(2) : null,
                      }),
                    'Asset added.',
                    () => setCreate(null),
                  )
                }
              >
                Add asset
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>

      <Modal opened={!!assign} onClose={() => setAssign(null)} title={assign ? `Hand over ${assign.asset.name}` : ''} centered>
        {assign ? (
          <Stack>
            <EmployeePicker value={assign.employeeId} onChange={(id) => setAssign({ ...assign, employeeId: id })} required />
            <DateField label="Handed over on" value={assign.on} onChange={(v) => setAssign({ ...assign, on: v })} />
            <Textarea
              label="Notes"
              value={assign.notes}
              onChange={(e) => setAssign({ ...assign, notes: e.currentTarget.value })}
              autosize
              minRows={1}
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setAssign(null)}>
                Cancel
              </Button>
              <Button
                loading={busy}
                disabled={!assign.employeeId}
                onClick={() =>
                  run(
                    () =>
                      lifecycle.assignAsset(assign.asset.id, {
                        employeeId: assign.employeeId,
                        ...(assign.on ? { assignedOn: assign.on } : {}),
                        notes: assign.notes || null,
                      }),
                    'Asset handed over.',
                    () => setAssign(null),
                  )
                }
              >
                Hand over
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>

      <Modal opened={!!ret} onClose={() => setRet(null)} title={ret ? `Return of ${ret.asset.name}` : ''} centered>
        {ret ? (
          <Stack>
            <Text size="sm" c="dimmed">
              From {ret.asset.employee_name}. Choose Lost if it was not returned; the asset is then retired.
            </Text>
            <Select
              label="Condition on return"
              value={ret.condition}
              allowDeselect={false}
              onChange={(v) => v && setRet({ ...ret, condition: v })}
              data={CONDITIONS}
            />
            <DateField label="Returned on" value={ret.on} onChange={(v) => setRet({ ...ret, on: v })} />
            <Textarea
              label="Notes"
              value={ret.notes}
              onChange={(e) => setRet({ ...ret, notes: e.currentTarget.value })}
              autosize
              minRows={1}
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setRet(null)}>
                Cancel
              </Button>
              <Button
                loading={busy}
                onClick={() =>
                  run(
                    () =>
                      lifecycle.returnAsset(ret.asset.id, {
                        condition: ret.condition,
                        ...(ret.on ? { returnedOn: ret.on } : {}),
                        notes: ret.notes || null,
                      }),
                    'Return recorded.',
                    () => setRet(null),
                  )
                }
              >
                Record return
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
