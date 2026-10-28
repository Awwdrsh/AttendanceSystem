import { Button, Group, Modal, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { addDays, fmtAd, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** "[2026-01-01,2026-04-01)" → "1 Jan 2026 to 31 Mar 2026"; open-ended ranges read "From 1 Jan 2026". */
function validLabel(range) {
  const m = /^([[(])(\d{4}-\d{2}-\d{2})?,\s*(\d{4}-\d{2}-\d{2})?([)\]])$/.exec(range.trim());
  if (!m) return range;
  const from = m[2] ? (m[1] === '(' ? addDays(m[2], 1) : m[2]) : null;
  const to = m[3] ? (m[4] === ')' ? addDays(m[3], -1) : m[3]) : null;
  if (from && to) return `${fmtAd(from)} to ${fmtAd(to)}`;
  if (from) return `From ${fmtAd(from)}`;
  return to ? `Until ${fmtAd(to)}` : 'Always';
}

export default function AssignmentsPage() {
  const [params] = useSearchParams();
  const [employeeId, setEmployeeId] = useState(params.get('employeeId'));
  const q = useQuery(() => payApi.assignments(employeeId ?? undefined), [employeeId]);
  const structs = useQuery(() => payApi.structures(), []);
  const [open, setOpen] = useState(false);
  const [emp, setEmp] = useState(employeeId);
  const [ver, setVer] = useState(null);
  const [from, setFrom] = useState(todayNpt());
  const [amounts, setAmounts] = useState({});
  const [busy, setBusy] = useState(false);
  const versions = (structs.data?.structures ?? []).flatMap((s) =>
    (s.versions ?? []).map((v) => ({ value: v.id, label: `${s.code} v${v.version}`, codes: v.components.map((c) => c.code) })),
  );
  const codes = versions.find((v) => v.value === ver)?.codes ?? [];
  const save = async () => {
    setBusy(true);
    try {
      const base = {};
      for (const [k, v] of Object.entries(amounts)) if (/^\d+(\.\d{1,2})?$/.test(v)) base[k] = v;
      await payApi.assign({ employeeId: emp, structureVersionId: ver, from: from, baseAmounts: base });
      toast.ok('Salary assigned.');
      setOpen(false);
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
        title="Salary assignments"
        subtitle="Which salary structure each person is on, and their fixed monthly amounts. A new assignment ends the previous one the day before it starts."
        actions={
          <>
            <EmployeePicker value={employeeId} onChange={setEmployeeId} label="" status="all" />
            <Button
              leftSection={<IconPlus size={16} stroke={1.6} />}
              onClick={() => {
                setEmp(employeeId);
                setOpen(true);
              }}
            >
              Assign salary
            </Button>
          </>
        }
      />
      <DataTable
        rows={q.data?.assignments ?? []}
        rowKey={(a) => `${a.employee_id}-${a.valid}`}
        loading={q.loading}
        error={q.error}
        emptyTitle="No salary assignments yet"
        emptyHint="Assign a salary structure and base amounts to each employee before running payroll."
        columns={[
          {
            key: 'emp',
            header: 'Employee',
            render: (a) => (
              <>
                <Text size="sm" fw={500}>
                  {a.name_en}
                </Text>
                <Text size="xs" c="dimmed">
                  {a.emp_code}
                </Text>
              </>
            ),
          },
          {
            key: 'structure',
            header: 'Structure',
            render: (a) => (
              <Text size="sm">
                {a.structure_code}{' '}
                <Text span c="dimmed" size="sm">
                  v{a.version}
                </Text>
              </Text>
            ),
          },
          {
            key: 'valid',
            header: 'Effective',
            render: (a) => (
              <Text size="sm" c="dimmed">
                {validLabel(a.valid)}
              </Text>
            ),
          },
          {
            key: 'amounts',
            header: 'Base amounts (NPR)',
            align: 'right',
            render: (a) => (
              <Stack gap={2} align="flex-end">
                {Object.entries(a.base_amounts).map(([k, v]) => (
                  <Group key={k} gap={8} wrap="nowrap">
                    <Text size="xs" c="dimmed">
                      {k}
                    </Text>
                    <Text size="sm" className="num">
                      {fmtMoney(v, { currency: false })}
                    </Text>
                  </Group>
                ))}
              </Stack>
            ),
          },
        ]}
      />
      <Modal opened={open} onClose={() => setOpen(false)} title="Assign salary structure" size="lg" centered>
        <Stack>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <EmployeePicker value={emp} onChange={setEmp} required />
            <Select
              label="Structure version"
              value={ver}
              onChange={setVer}
              data={versions.map((v) => ({ value: v.value, label: v.label }))}
              required
            />
            <DateField label="Effective from" value={from} onChange={setFrom} required />
          </SimpleGrid>
          {codes.length ? (
            <>
              <div>
                <Text size="sm" fw={500}>
                  Monthly amounts (NPR)
                </Text>
                <Text size="xs" c="dimmed">
                  Fill in fixed amounts such as BASIC, DA and ALLOWANCE. Leave calculated components (SSF, TDS, overtime) blank. Minimum
                  wage: basic at least 12,170 and basic plus DA at least 19,550.
                </Text>
              </div>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                {codes.map((c) => (
                  <TextInput
                    key={c}
                    label={c}
                    value={amounts[c] ?? ''}
                    onChange={(e) => setAmounts({ ...amounts, [c]: e.currentTarget.value })}
                    placeholder="e.g. 40000.00"
                  />
                ))}
              </SimpleGrid>
            </>
          ) : (
            <Text size="sm" c="dimmed">
              Pick a structure version to enter its amounts.
            </Text>
          )}
          <Group justify="flex-end" mt="xs">
            <Button variant="default" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={!emp || !ver || !from} onClick={save}>
              Assign salary
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
