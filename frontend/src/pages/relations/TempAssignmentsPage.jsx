import { Badge, Button, Group, Menu, Modal, SegmentedControl, Select, Stack, Text, Textarea } from '@mantine/core';
import { IconDots, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { people } from '../../api/people';
import { relations } from '../../api/relations';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export function AssignmentForm({ opened, onClose, onSaved, employeeId }) {
  const structure = useQuery(() => people.structure(), [opened], { enabled: opened });
  const [f, setF] = useState({
    emp: employeeId ?? null,
    kind: 'acting',
    designationId: null,
    branchId: null,
    orgUnitId: null,
    start: todayNpt(),
    end: null,
    note: '',
  });
  const [busy, setBusy] = useState(false);
  const s = structure.data;
  const ok = !!f.emp && !!f.start && !!f.end && (f.kind === 'acting' ? !!f.designationId : !!(f.branchId || f.orgUnitId));
  return (
    <Modal opened={opened} onClose={onClose} title="Acting or deputation" centered size="lg">
      <Stack>
        {employeeId ? null : <EmployeePicker value={f.emp} onChange={(v) => setF({ ...f, emp: v })} required />}
        <SegmentedControl
          value={f.kind}
          onChange={(v) => setF({ ...f, kind: v })}
          data={[
            { value: 'acting', label: 'Acting in a position' },
            { value: 'deputation', label: 'Deputed elsewhere' },
          ]}
        />
        {f.kind === 'acting' ? (
          <Select
            label="Acting as"
            searchable
            required
            value={f.designationId}
            onChange={(v) => setF({ ...f, designationId: v })}
            data={(s?.designations ?? []).filter((d) => d.active).map((d) => ({ value: d.id, label: d.name_en }))}
          />
        ) : (
          <Group grow>
            <Select
              label="Branch"
              clearable
              value={f.branchId}
              onChange={(v) => setF({ ...f, branchId: v })}
              data={(s?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }))}
            />
            <Select
              label="Department"
              clearable
              searchable
              value={f.orgUnitId}
              onChange={(v) => setF({ ...f, orgUnitId: v })}
              data={(s?.units ?? []).map((u) => ({ value: u.id, label: u.name_en }))}
            />
          </Group>
        )}
        <Group grow>
          <DateField label="From" value={f.start} onChange={(v) => setF({ ...f, start: v })} required />
          <DateField
            label="Until"
            value={f.end}
            onChange={(v) => setF({ ...f, end: v })}
            {...(f.start ? { minDate: f.start } : {})}
            required
          />
        </Group>
        <Textarea
          label="Note (optional)"
          value={f.note}
          onChange={(e) => setF({ ...f, note: e.currentTarget.value })}
          autosize
          maxLength={500}
        />
        <Text size="xs" c="dimmed">
          The permanent designation and placement do not change. The assignment shows in the directory and ends by itself.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!ok}
            onClick={async () => {
              setBusy(true);
              try {
                await relations.addAssignment({
                  employeeId: f.emp,
                  kind: f.kind,
                  startDate: f.start,
                  endDate: f.end,
                  note: f.note.trim() || null,
                  ...(f.kind === 'acting' ? { designationId: f.designationId } : { branchId: f.branchId, orgUnitId: f.orgUnitId }),
                });
                toast.ok('Saved.');
                onSaved();
                onClose();
              } catch (e) {
                toast.err(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export const assignmentWhere = (a) =>
  a.kind === 'acting' ? (a.designation ?? '') : [a.branch_name, a.unit_name].filter(Boolean).join(', ');

/** HR: people acting in another position or deputed to another branch or department for a while. */
export default function TempAssignmentsPage() {
  const [status, setStatus] = useState('active');
  const q = useQuery(() => relations.assignments({ status }), [status]);
  const [adding, setAdding] = useState(false);
  const end = async (a, cancel) => {
    try {
      await relations.endAssignment(a.id, cancel ? { cancel: true } : {});
      toast.ok(cancel ? 'Cancelled.' : 'Ended today.');
      await q.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Stack gap="lg">
      <PageHeader
        title="Acting and deputation"
        subtitle="Temporary roles: someone acting as a department head while the post is vacant, or deputed to another branch."
        actions={
          <>
            <SegmentedControl
              value={status}
              onChange={(v) => setStatus(v)}
              data={[
                { value: 'active', label: 'Current' },
                { value: 'all', label: 'All' },
              ]}
            />
            <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setAdding(true)}>
              Add
            </Button>
          </>
        }
      />
      <DataTable
        rows={q.data?.assignments ?? []}
        rowKey={(r) => r.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No temporary roles"
        emptyHint="Add one when someone covers a post or works from another branch for a period."
        columns={[
          {
            key: 'e',
            header: 'Employee',
            render: (r) => (
              <>
                <Text size="sm">{r.employee_name}</Text>
                <Text size="xs" c="dimmed">
                  {r.emp_code}
                </Text>
              </>
            ),
          },
          {
            key: 'k',
            header: 'Type',
            render: (r) => (
              <Badge variant="light" color={r.kind === 'acting' ? 'violet' : 'cyan'}>
                {r.kind === 'acting' ? 'Acting' : 'Deputation'}
              </Badge>
            ),
          },
          { key: 'w', header: 'Position or place', render: assignmentWhere },
          { key: 'p', header: 'Period', render: (r) => `${fmtAd(r.start_date)} to ${fmtAd(r.end_date)}` },
          {
            key: 's',
            header: 'Status',
            render: (r) => (
              <Badge variant="light" color={r.status === 'active' ? 'teal' : 'gray'}>
                {r.status === 'active' ? 'Current' : r.status === 'ended' ? 'Ended' : 'Cancelled'}
              </Badge>
            ),
          },
          {
            key: 'a',
            header: '',
            align: 'right',
            render: (r) =>
              r.status === 'active' ? (
                <Menu position="bottom-end">
                  <Menu.Target>
                    <Button size="compact-xs" variant="subtle" color="gray">
                      <IconDots size={16} />
                    </Button>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item onClick={() => end(r, false)}>End today</Menu.Item>
                    <Menu.Item color="red" onClick={() => end(r, true)}>
                      Cancel
                    </Menu.Item>
                  </Menu.Dropdown>
                </Menu>
              ) : null,
          },
        ]}
      />
      <AssignmentForm
        opened={adding}
        onClose={() => setAdding(false)}
        onSaved={() => {
          void q.reload();
        }}
      />
    </Stack>
  );
}
