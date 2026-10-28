import {
  Alert,
  Badge,
  Button,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconAlertTriangle, IconArrowBackUp, IconCheck, IconLock } from '@tabler/icons-react';
import { adToBs } from '@/lib/bs-calendar';
import { useEffect, useState } from 'react';
import { leave as leaveApi } from '../../api';
import { attendanceTools } from '../../api/attendanceTools';
import { BsMonthPicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { bsMonthLabel, fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const days = (n) => `${n} ${n === 1 ? 'day' : 'days'}`;

/** HR: attendance rules (late penalty, blocking late clock-ins, who's out) and the monthly late-arrival penalty run. */
export default function LatePenaltiesPage() {
  const now = adToBs(todayNpt());
  const [month, setMonth] = useState({ year: now.year, month: now.month });
  const policy = useQuery(() => attendanceTools.policy(), []);
  const types = useQuery(() => leaveApi.types(), []);
  const lines = useQuery(() => attendanceTools.penalties(month.year, month.month), [month.year, month.month]);
  const [draft, setDraft] = useState(null);
  const [saving, setSaving] = useState(false);
  const [selected, setSelected] = useState(new Set());
  const [applying, setApplying] = useState(false);
  const [reverse, setReverse] = useState(null);
  const [reason, setReason] = useState('');

  useEffect(() => {
    if (policy.data) setDraft(policy.data.policy);
  }, [policy.data]);
  useEffect(() => {
    setSelected(new Set());
  }, [month.year, month.month]);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      const r = await attendanceTools.savePolicy(draft);
      policy.setData(r);
      toast.ok('Attendance rules saved.');
      await lines.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setSaving(false);
    }
  };
  const lp = draft?.latePenalty;
  const setLp = (k, v) => setDraft((d) => d && { ...d, latePenalty: { ...d.latePenalty, [k]: v } });
  const paidTypes = (types.data?.types ?? []).filter((t) => t.paid && !['MATERNITY', 'PATERNITY', 'MOURNING'].includes(t.code));

  const rows = lines.data?.lines ?? [];
  const due = rows.filter((l) => l.penaltyDays > 0 && !l.applied);
  const locked = !!lines.data?.locked;
  const enabled = !!lines.data?.rule.enabled;
  const apply = async (ids) => {
    setApplying(true);
    try {
      const r = await attendanceTools.applyPenalties(month.year, month.month, ids);
      toast.ok(
        `${r.applied} penalt${r.applied === 1 ? 'y' : 'ies'} applied${r.skipped ? `, ${r.skipped} skipped` : ''}. The employees have been told.`,
      );
      setSelected(new Set());
      await lines.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setApplying(false);
    }
  };

  return (
    <Stack gap="xl">
      <Paper withBorder radius="lg" p="lg">
        <SectionTitle
          hint="Rules for late arrivals and clock-ins. They apply to the whole company."
          actions={
            <Button size="xs" loading={saving} disabled={!draft} onClick={save}>
              Save rules
            </Button>
          }
        >
          Attendance rules
        </SectionTitle>
        {draft && lp ? (
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="xl">
            <Stack gap="sm">
              <Switch
                label="Late-arrival penalty"
                description="Repeated late arrivals in a month cost leave, or pay when there is no leave left"
                checked={lp.enabled}
                onChange={(e) => setLp('enabled', e.currentTarget.checked)}
              />
              <Group grow align="flex-start">
                <NumberInput
                  label="Every"
                  suffix=" late days"
                  min={1}
                  max={31}
                  value={lp.lateCount}
                  onChange={(v) => setLp('lateCount', Number(v) || 1)}
                  disabled={!lp.enabled}
                />
                <NumberInput
                  label="Costs"
                  suffix=" day(s)"
                  min={0.25}
                  max={5}
                  step={0.25}
                  decimalScale={2}
                  value={lp.penaltyDays}
                  onChange={(v) => setLp('penaltyDays', Number(v) || 0.25)}
                  disabled={!lp.enabled}
                />
              </Group>
              <NumberInput
                label="Count a day as late when arriving at least"
                suffix=" min after the grace time"
                min={1}
                max={240}
                value={lp.minLateMinutes}
                onChange={(v) => setLp('minLateMinutes', Number(v) || 1)}
                disabled={!lp.enabled}
              />
              <MultiSelect
                label="Take the penalty from"
                description="In this order; what these cannot cover is unpaid"
                placeholder="Unpaid only"
                maxValues={5}
                data={paidTypes.map((t) => ({ value: t.id, label: t.name_en }))}
                value={lp.leaveTypeIds}
                onChange={(v) => setLp('leaveTypeIds', v)}
                disabled={!lp.enabled}
              />
            </Stack>
            <Stack gap="sm">
              <Switch
                label="Close clock-in after the grace time"
                description="A first clock-in later than the shift start and grace is refused; the employee raises a time request with the time they arrived instead"
                checked={draft.blockLateClockIn}
                onChange={(e) => setDraft({ ...draft, blockLateClockIn: e.currentTarget.checked })}
              />
              <NumberInput
                label="Extra minutes after the grace time"
                min={0}
                max={240}
                value={draft.blockAfterMin}
                onChange={(v) => setDraft({ ...draft, blockAfterMin: Number(v) || 0 })}
                disabled={!draft.blockLateClockIn}
              />
              <Select
                label="Who's out shows"
                allowDeselect={false}
                value={draft.whosOutScope}
                onChange={(v) => v && setDraft({ ...draft, whosOutScope: v })}
                data={[
                  { value: 'team', label: "Each person's team" },
                  { value: 'department', label: "Each person's department" },
                  { value: 'company', label: 'The whole company' },
                ]}
                description="The Who's out card on Home. Leave types are never shown, only that someone is away."
              />
            </Stack>
          </SimpleGrid>
        ) : (
          <Text size="sm" c="dimmed">
            Loading…
          </Text>
        )}
      </Paper>

      <Stack gap="md">
        <SectionTitle
          hint="Late days per person for the month and what the penalty would be. Apply before running payroll; unpaid days are deducted in the pay run."
          actions={
            <Group gap="xs" align="flex-end">
              <BsMonthPicker value={month} onChange={setMonth} />
              <Button
                leftSection={<IconCheck size={16} stroke={1.6} />}
                disabled={!enabled || locked || !due.length}
                loading={applying}
                onClick={() => void apply(selected.size ? [...selected] : undefined)}
              >
                {selected.size ? `Apply ${selected.size} selected` : `Apply all (${due.length})`}
              </Button>
            </Group>
          }
        >
          Late-arrival penalties, {bsMonthLabel(month.year, month.month)}
        </SectionTitle>
        {!enabled ? (
          <Alert variant="light" color="gray" icon={<IconAlertTriangle size={16} stroke={1.6} />}>
            The late-arrival penalty is off. Late days are still listed so you can see who is often late.
          </Alert>
        ) : null}
        {locked ? (
          <Alert variant="light" color="gray" icon={<IconLock size={16} stroke={1.6} />}>
            This month's payroll is locked; penalties can no longer be applied or reversed.
          </Alert>
        ) : null}
        <StatStrip
          stats={[
            { key: 'late', label: 'People late', value: rows.length },
            {
              key: 'due',
              label: 'Penalties to apply',
              value: enabled ? due.length : 0,
              ...(due.length && enabled ? { tone: 'warn' } : {}),
            },
            { key: 'applied', label: 'Applied', value: rows.filter((l) => l.applied).length },
            { key: 'unpaid', label: 'Unpaid days', value: rows.reduce((s, l) => s + (l.applied ? l.applied.unpaidDays : l.unpaidDays), 0) },
          ]}
        />
        <DataTable
          rows={rows}
          rowKey={(l) => l.employeeId}
          loading={lines.loading}
          error={lines.error}
          emptyTitle="Nobody was late this month"
          emptyHint="Late days come from attendance: arrivals after the shift start and its grace time."
          selectable={enabled && !locked}
          selected={selected}
          onSelect={(ids) => setSelected(new Set([...ids].filter((id) => due.some((d) => d.employeeId === id))))}
          columns={[
            {
              key: 'emp',
              header: 'Name',
              sticky: true,
              render: (l) => (
                <div>
                  <Text size="sm" fw={500}>
                    {l.name}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {l.empCode}
                  </Text>
                </div>
              ),
            },
            {
              key: 'late',
              header: 'Late days',
              align: 'right',
              render: (l) => (
                <Tooltip multiline w={260} label={l.lateDates.map(fmtDateShort).join(', ')}>
                  <Text size="sm" className="num">
                    {l.lateDays}
                  </Text>
                </Tooltip>
              ),
            },
            {
              key: 'min',
              header: 'Minutes late',
              align: 'right',
              render: (l) => (
                <Text size="sm" c="dimmed" className="num">
                  {l.lateMinutes}
                </Text>
              ),
            },
            {
              key: 'pen',
              header: 'Penalty',
              align: 'right',
              render: (l) =>
                l.penaltyDays ? (
                  <Text size="sm" fw={500} className="num">
                    {days(l.penaltyDays)}
                  </Text>
                ) : (
                  <Text size="sm" c="dimmed">
                    None
                  </Text>
                ),
            },
            {
              key: 'split',
              header: 'Taken from',
              render: (l) => {
                const a = l.applied;
                if (!l.penaltyDays && !a)
                  return (
                    <Text size="sm" c="dimmed">
                      —
                    </Text>
                  );
                const leave = a ? a.leaveDays : l.allocations.reduce((s, x) => s + x.days, 0);
                const unpaid = a ? a.unpaidDays : l.unpaidDays;
                return (
                  <Group gap={6}>
                    {leave ? (
                      <Badge variant="light" color="blue" size="sm">
                        {days(leave)} leave{a ? '' : ` (${l.allocations.map((x) => x.code).join(', ')})`}
                      </Badge>
                    ) : null}
                    {unpaid ? (
                      <Badge variant="light" color="red" size="sm">
                        {days(unpaid)} unpaid
                      </Badge>
                    ) : null}
                  </Group>
                );
              },
            },
            {
              key: 'status',
              header: 'Status',
              render: (l) =>
                l.applied ? (
                  <Group gap={6} wrap="nowrap">
                    <Badge color="teal" variant="light" size="sm">
                      Applied
                    </Badge>
                    {!locked ? (
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="gray"
                        leftSection={<IconArrowBackUp size={12} />}
                        onClick={() => {
                          setReverse(l);
                          setReason('');
                        }}
                      >
                        Reverse
                      </Button>
                    ) : null}
                  </Group>
                ) : l.penaltyDays ? (
                  enabled ? (
                    <Badge color="orange" variant="light" size="sm">
                      To apply
                    </Badge>
                  ) : (
                    <Text size="sm" c="dimmed">
                      Rule off
                    </Text>
                  )
                ) : (
                  <Text size="sm" c="dimmed">
                    Under the limit
                  </Text>
                ),
            },
          ]}
        />
      </Stack>

      <Modal opened={!!reverse} onClose={() => setReverse(null)} centered title={<Text fw={600}>Reverse penalty for {reverse?.name}</Text>}>
        <Stack>
          <Text size="sm">
            The leave taken is given back and the unpaid days are removed from this month's pay run. The employee keeps the record of the
            late days.
          </Text>
          <TextInput
            label="Reason"
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value)}
            placeholder="For example: the device clock was wrong"
            data-autofocus
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setReverse(null)}>
              Cancel
            </Button>
            <Button
              disabled={reason.trim().length < 3}
              onClick={async () => {
                try {
                  await attendanceTools.reversePenalty(reverse.applied.id, reason.trim());
                  toast.ok('Penalty reversed.');
                  setReverse(null);
                  await lines.reload();
                } catch (e) {
                  toast.err(e);
                }
              }}
            >
              Reverse penalty
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
