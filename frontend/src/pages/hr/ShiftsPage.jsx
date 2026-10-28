import { ActionIcon, Button, Drawer, Group, Menu, Modal, NumberInput, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconDots, IconPencil, IconPlus, IconTrash, IconUserPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };

const blank = {
  code: '',
  nameEn: '',
  nameNe: '',
  start: '10:00',
  end: '18:00',
  graceInMin: 15,
  graceOutMin: 0,
  earlyInWindowMin: 60,
  lateOutWindowMin: 120,
  breakMinutes: 0,
  halfDayThresholdMin: 240,
  absentThresholdMin: 120,
  pairingMode: 'alternating',
  roundingMin: 0,
  roundingDirection: 'nearest',
};

const toForm = (s) => ({
  id: s.id,
  code: s.code,
  nameEn: s.name_en,
  nameNe: s.name_ne ?? '',
  start: s.start.slice(0, 5),
  end: s.end.slice(0, 5),
  graceInMin: s.grace_in_min,
  graceOutMin: s.grace_out_min,
  earlyInWindowMin: s.early_in_window_min,
  lateOutWindowMin: s.late_out_window_min,
  breakMinutes: s.break_minutes,
  halfDayThresholdMin: s.half_day_threshold_min,
  absentThresholdMin: s.absent_threshold_min,
  pairingMode: s.pairing_mode,
  roundingMin: s.rounding_min,
  roundingDirection: s.rounding_direction,
});

export default function ShiftsPage() {
  const q = useQuery(() => shiftApi.list(), []);
  const [edit, setEdit] = useState(null);
  const [del, setDel] = useState(null);
  const [assign, setAssign] = useState(null);
  const [assignEmp, setAssignEmp] = useState(null);
  const [assignFrom, setAssignFrom] = useState(todayNpt());
  const [busy, setBusy] = useState(false);
  const n = (k) => ({ value: edit?.[k], onChange: (v) => setEdit((e) => e && { ...e, [k]: Number(v) }), min: 0, suffix: ' min' });
  const save = async () => {
    if (!edit) return;
    setBusy(true);
    try {
      const { id, ...body } = edit;
      if (id) await shiftApi.update(id, body);
      else await shiftApi.create(body);
      toast.ok('Shift saved.');
      setEdit(null);
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="lg">
      <PageHeader
        title="Shifts"
        subtitle="Working hours and the rules used to mark people late, half day or absent."
        actions={
          <Button leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ ...blank })}>
            New shift
          </Button>
        }
      />
      <DataTable
        rows={q.data?.shifts ?? []}
        rowKey={(s) => s.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No shifts yet"
        emptyHint="Create your first shift. Most companies start with a general 10:00 – 18:00 shift."
        onRowClick={(s) => setEdit(toForm(s))}
        columns={[
          {
            key: 'name',
            header: 'Shift',
            render: (s) => (
              <>
                <Text size="sm" fw={500}>
                  {s.name_en}
                </Text>
                <Text size="xs" c="dimmed" ff="monospace">
                  {s.code}
                </Text>
              </>
            ),
          },
          {
            key: 'time',
            header: 'Hours',
            render: (s) => (
              <>
                <Text size="sm" className="num" fw={500}>
                  {s.start.slice(0, 5)} – {s.end.slice(0, 5)}
                  {s.crosses_midnight ? (
                    <Text span size="xs" c="dimmed">
                      {' '}
                      next day
                    </Text>
                  ) : null}
                </Text>
                <Text size="xs" c="dimmed">
                  {s.grace_in_min} min grace{s.break_minutes > 0 ? `, ${s.break_minutes} min break` : ''}
                </Text>
              </>
            ),
          },
          {
            key: 'thr',
            header: 'Half day / absent under',
            render: (s) => (
              <Text size="sm" c="dimmed" className="num">
                {minutesToHm(s.half_day_threshold_min)} / {minutesToHm(s.absent_threshold_min)}
              </Text>
            ),
          },
          { key: 'n', header: 'Employees', render: (s) => <span className="num">{s.assigned_employees}</span>, align: 'right' },
          {
            key: 'a',
            header: '',
            align: 'right',
            width: 56,
            render: (s) => (
              <Group justify="flex-end" onClick={(e) => e.stopPropagation()}>
                <Menu position="bottom-end" withinPortal>
                  <Menu.Target>
                    <ActionIcon variant="subtle" color="gray" aria-label="Shift actions">
                      <IconDots {...ICON} />
                    </ActionIcon>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item leftSection={<IconPencil {...ICON} />} onClick={() => setEdit(toForm(s))}>
                      Edit shift
                    </Menu.Item>
                    <Menu.Item leftSection={<IconUserPlus {...ICON} />} onClick={() => setAssign(s)}>
                      Assign to employee
                    </Menu.Item>
                    <Menu.Divider />
                    <Menu.Item color="red" leftSection={<IconTrash {...ICON} />} onClick={() => setDel(s)}>
                      Delete shift
                    </Menu.Item>
                  </Menu.Dropdown>
                </Menu>
              </Group>
            ),
          },
        ]}
      />

      <Drawer
        opened={!!edit}
        onClose={() => setEdit(null)}
        position="right"
        size="md"
        title={<Text fw={600}>{edit?.id ? `Edit ${edit.nameEn || edit.code}` : 'New shift'}</Text>}
      >
        {edit ? (
          <Stack gap="lg">
            <div>
              <SectionTitle>Basics</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <TextInput
                  label="Code"
                  value={edit.code}
                  onChange={(e) => setEdit({ ...edit, code: e.currentTarget.value.toUpperCase() })}
                  required
                />
                <TextInput
                  label="Name"
                  value={edit.nameEn}
                  onChange={(e) => setEdit({ ...edit, nameEn: e.currentTarget.value })}
                  required
                />
                <TextInput
                  label="Starts at"
                  placeholder="HH:MM"
                  value={edit.start}
                  onChange={(e) => setEdit({ ...edit, start: e.currentTarget.value })}
                />
                <TextInput
                  label="Ends at"
                  placeholder="HH:MM"
                  description="Earlier than the start means a night shift"
                  value={edit.end}
                  onChange={(e) => setEdit({ ...edit, end: e.currentTarget.value })}
                />
              </SimpleGrid>
            </div>
            <div>
              <SectionTitle>Grace and windows</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <NumberInput label="Grace on arrival" description="Late if clock-in is after start plus grace" {...n('graceInMin')} />
                <NumberInput label="Grace on leaving" {...n('graceOutMin')} />
                <NumberInput label="Early clock-in window" description="How early a clock-in still counts" {...n('earlyInWindowMin')} />
                <NumberInput label="Late clock-out window" description="How late a clock-out still counts" {...n('lateOutWindowMin')} />
                <NumberInput
                  label="Fixed break"
                  description="30 min after 5 h (Sec 28(2)); 0 measures it from punches"
                  {...n('breakMinutes')}
                />
              </SimpleGrid>
            </div>
            <div>
              <SectionTitle>Thresholds</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <NumberInput label="Half day if worked under" {...n('halfDayThresholdMin')} />
                <NumberInput label="Absent if worked under" {...n('absentThresholdMin')} />
              </SimpleGrid>
            </div>
            <div>
              <SectionTitle>Punch handling</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <Select
                  label="Pairing"
                  description="How punches become in/out pairs"
                  value={edit.pairingMode}
                  onChange={(v) => v && setEdit({ ...edit, pairingMode: v })}
                  allowDeselect={false}
                  data={[
                    { value: 'alternating', label: 'Alternating in/out' },
                    { value: 'strict_log_type', label: 'Use device direction' },
                    { value: 'first_last', label: 'First in to last out' },
                  ]}
                />
                <NumberInput label="Round worked time to" description="0 turns rounding off" {...n('roundingMin')} />
                <Select
                  label="Rounding direction"
                  value={edit.roundingDirection}
                  onChange={(v) => v && setEdit({ ...edit, roundingDirection: v })}
                  allowDeselect={false}
                  data={[
                    { value: 'nearest', label: 'Nearest' },
                    { value: 'down', label: 'Down' },
                    { value: 'up', label: 'Up' },
                  ]}
                />
              </SimpleGrid>
            </div>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEdit(null)}>
                Cancel
              </Button>
              <Button loading={busy} onClick={save}>
                Save shift
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>

      <Modal opened={!!assign} onClose={() => setAssign(null)} title={`Assign ${assign?.name_en ?? ''}`} centered>
        <Stack>
          <EmployeePicker value={assignEmp} onChange={setAssignEmp} required />
          <DateField label="Starting from" value={assignFrom} onChange={setAssignFrom} required />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setAssign(null)}>
              Cancel
            </Button>
            <Button
              disabled={!assignEmp || !assignFrom}
              onClick={async () => {
                if (!assign || !assignEmp || !assignFrom) return;
                try {
                  await shiftApi.assign(assignEmp, assign.id, assignFrom);
                  toast.ok('Shift assigned.');
                  setAssign(null);
                  await q.reload();
                } catch (e) {
                  toast.err(e);
                }
              }}
            >
              Assign shift
            </Button>
          </Group>
        </Stack>
      </Modal>
      <ConfirmDialog
        opened={!!del}
        onClose={() => setDel(null)}
        title={`Delete ${del?.name_en ?? 'shift'}?`}
        confirmLabel="Delete shift"
        message={
          Number(del?.assigned_employees ?? 0) > 0
            ? `${del?.assigned_employees} employee(s) are on this shift. Move them to another shift first, otherwise the delete is refused.`
            : 'Nobody is on this shift. Past attendance keeps its data.'
        }
        onConfirm={async () => {
          if (!del) return;
          try {
            await shiftApi.remove(del.id);
            toast.ok('Shift deleted.');
            await q.reload();
          } catch (e) {
            toast.err(e);
          }
        }}
      />
    </Stack>
  );
}
