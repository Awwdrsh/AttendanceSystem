import {
  Anchor,
  Badge,
  Button,
  Checkbox,
  Drawer,
  Group,
  Modal,
  NumberInput,
  Paper,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconPlus, IconSettings } from '@tabler/icons-react';
import { useState } from 'react';
import { documents } from '../../api/documents';
import { CASE_CATEGORY_LABEL, CASE_STATUS_LABEL, relations } from '../../api/relations';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { addDays, fmtAd, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const CATEGORIES = Object.keys(CASE_CATEGORY_LABEL).map((v) => ({ value: v, label: CASE_CATEGORY_LABEL[v] }));
const STATUS_COLOR = { open: 'blue', show_cause: 'orange', responded: 'violet', decided: 'teal', withdrawn: 'gray' };
export const CaseStatusBadge = ({ status }) => (
  <Badge variant="light" color={STATUS_COLOR[status]}>
    {CASE_STATUS_LABEL[status]}
  </Badge>
);

function OpenCase({ opened, onClose, onCreated, employeeId }) {
  const [emp, setEmp] = useState(employeeId ?? null);
  const [category, setCategory] = useState(null);
  const [date, setDate] = useState(todayNpt());
  const [incident, setIncident] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title="Open a disciplinary case" centered size="lg">
      <Stack>
        {employeeId ? null : <EmployeePicker value={emp} onChange={setEmp} required />}
        <Group grow>
          <Select label="Category" data={CATEGORIES} value={category} onChange={(v) => setCategory(v)} allowDeselect={false} required />
          <DateField label="Incident date" value={date} onChange={setDate} maxDate={todayNpt()} required />
        </Group>
        <Textarea
          label="What happened"
          description="Kept with HR. The employee sees this only if you send a show-cause notice that quotes it."
          value={incident}
          onChange={(e) => setIncident(e.currentTarget.value)}
          autosize
          minRows={4}
          maxLength={8000}
          required
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!emp || !category || !date || !incident.trim()}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await relations.openCase({ employeeId: emp, category: category, incidentDate: date, incident: incident.trim() });
                toast.ok(`Case D-${r.number} opened.`);
                setIncident('');
                onCreated(r.id);
              } catch (e) {
                toast.err(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Open case
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** One case for HR: the incident, the show-cause notice and response, the decision with its effect, and the warning letter. */
export function CaseDetail({ id, onChanged }) {
  const q = useQuery(() => relations.case(id), [id]);
  const stages = useQuery(() => relations.stages(), []);
  const [showCause, setShowCause] = useState(null);
  const [decide, setDecide] = useState(null);
  const [withdraw, setWithdraw] = useState(null);
  const [busy, setBusy] = useState(false);
  if (q.error) return <EmptyState title="Could not open this case" hint={q.error} />;
  if (!q.data)
    return (
      <Text size="sm" c="dimmed">
        Loading…
      </Text>
    );
  const k = q.data.case;
  const stage = stages.data?.stages.find((s) => s.id === decide?.stageId) ?? null;
  const decidedStage = stages.data?.stages.find((s) => s.id === k.stageId) ?? null;
  const run = async (fn, msg) => {
    setBusy(true);
    try {
      await fn();
      toast.ok(msg);
      await q.reload();
      onChanged?.();
      return true;
    } catch (e) {
      toast.err(e);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const issueLetter = async () => {
    const code = decidedStage?.letter_template_code ?? 'warning';
    const tpl = (await documents.templates()).templates.find((t) => t.code === code);
    if (!tpl) {
      toast.err(new Error(`No "${code}" letter template. Add one under Employees, Documents & letters.`));
      return;
    }
    await run(async () => {
      const l = await documents.issue({
        templateId: tpl.id,
        employeeId: k.employeeId,
        purpose: `${CASE_CATEGORY_LABEL[k.category].toLowerCase()} (case D-${k.number})`,
        allowMissing: true,
      });
      await relations.linkLetter(k.id, l.id);
    }, 'Letter issued and linked to the case.');
  };
  const open = ['open', 'show_cause', 'responded'].includes(k.status);
  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <div>
          <Text size="xs" c="dimmed" ff="monospace">
            D-{k.number}
          </Text>
          <Text fw={600}>
            {k.employeeName}{' '}
            <Text span c="dimmed" size="sm">
              {k.empCode}
            </Text>
          </Text>
          <Text size="xs" c="dimmed">
            {CASE_CATEGORY_LABEL[k.category]}, incident on {fmtAd(k.incidentDate)}
          </Text>
        </div>
        <CaseStatusBadge status={k.status} />
      </Group>
      <Paper withBorder p="sm">
        <Text size="xs" c="dimmed" mb={4}>
          Incident (HR only)
        </Text>
        <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
          {k.incident}
        </Text>
      </Paper>
      {k.showCause ? (
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed" mb={4}>
            Show-cause notice, reply by {fmtAd(k.responseDue)}
          </Text>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {k.showCause}
          </Text>
        </Paper>
      ) : null}
      {k.response ? (
        <Paper withBorder p="sm">
          <Text size="xs" c="dimmed" mb={4}>
            Employee's response, {relativeTime(k.respondedAt)}
            {k.responseDue && k.respondedAt && k.respondedAt.slice(0, 10) > k.responseDue ? ' (late)' : ''}
          </Text>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {k.response}
          </Text>
        </Paper>
      ) : k.status === 'show_cause' ? (
        <Text size="sm" c="dimmed">
          Waiting for the employee to respond.
        </Text>
      ) : null}
      {k.status === 'decided' || k.status === 'withdrawn' ? (
        <Paper withBorder p="sm">
          <Group gap="xs" mb={4}>
            <Text size="sm" fw={600}>
              {k.status === 'withdrawn' ? 'Withdrawn' : (k.stageName ?? 'No action')}
            </Text>
            {k.suspensionFrom ? (
              <Badge variant="light" color="orange">
                Suspended {fmtAd(k.suspensionFrom)} to {fmtAd(k.suspensionTo)}
              </Badge>
            ) : null}
            {k.exitId ? (
              <Badge variant="light" color="red">
                Exit recorded
              </Badge>
            ) : null}
          </Group>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {k.decisionNote}
          </Text>
          {k.letterId ? (
            <Anchor size="sm" href={documents.letterUrl(k.letterId)} target="_blank" rel="noopener">
              Letter {k.letterRef}
            </Anchor>
          ) : k.status === 'decided' && k.stageId ? (
            <Button mt="xs" size="xs" variant="default" loading={busy} onClick={issueLetter}>
              Issue {decidedStage?.letter_template_code ? 'the' : 'a warning'} letter
            </Button>
          ) : null}
        </Paper>
      ) : null}

      {open ? (
        <Group gap="xs">
          {k.status === 'open' ? (
            <Button size="xs" variant="default" onClick={() => setShowCause({ text: '', due: addDays(todayNpt(), 7) })}>
              Send show-cause notice
            </Button>
          ) : null}
          <Button size="xs" onClick={() => setDecide({ stageId: null, note: '', from: null, to: null, lwd: null, force: false })}>
            Decide
          </Button>
          <Button size="xs" variant="subtle" color="gray" onClick={() => setWithdraw('')}>
            Withdraw
          </Button>
        </Group>
      ) : null}
      {showCause ? (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Textarea
              label="Notice to the employee"
              description="Describe the incident and ask them to explain. They read this in self-service."
              value={showCause.text}
              onChange={(e) => setShowCause({ ...showCause, text: e.currentTarget.value })}
              autosize
              minRows={4}
              maxLength={8000}
            />
            <DateField
              label="Reply by"
              value={showCause.due}
              onChange={(v) => setShowCause({ ...showCause, due: v })}
              minDate={todayNpt()}
              required
            />
            <Group justify="flex-end">
              <Button size="xs" variant="default" onClick={() => setShowCause(null)}>
                Cancel
              </Button>
              <Button
                size="xs"
                loading={busy}
                disabled={!showCause.text.trim() || !showCause.due}
                onClick={async () => {
                  if (
                    await run(
                      () => relations.showCause(k.id, { text: showCause.text.trim(), responseDue: showCause.due }),
                      'Notice sent to the employee.',
                    )
                  )
                    setShowCause(null);
                }}
              >
                Send notice
              </Button>
            </Group>
          </Stack>
        </Paper>
      ) : null}
      {decide ? (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Select
              label="Action"
              placeholder="No action"
              clearable
              value={decide.stageId}
              onChange={(v) => setDecide({ ...decide, stageId: v })}
              data={(stages.data?.stages ?? []).filter((s) => s.active).map((s) => ({ value: s.id, label: s.name }))}
            />
            {stage?.effect === 'suspension' ? (
              <Group grow>
                <DateField label="Suspended from" value={decide.from} onChange={(v) => setDecide({ ...decide, from: v })} required />
                <DateField
                  label="Until"
                  value={decide.to}
                  onChange={(v) => setDecide({ ...decide, to: v })}
                  {...(decide.from ? { minDate: decide.from } : {})}
                  required
                />
              </Group>
            ) : null}
            {stage?.effect === 'termination' ? (
              <DateField
                label="Last working day"
                description="Starts the exit and clearance under Employees, Exits."
                value={decide.lwd}
                onChange={(v) => setDecide({ ...decide, lwd: v })}
                required
              />
            ) : null}
            <Textarea
              label="Decision and reasons"
              description="The employee reads this."
              value={decide.note}
              onChange={(e) => setDecide({ ...decide, note: e.currentTarget.value })}
              autosize
              minRows={3}
              maxLength={8000}
            />
            {k.status === 'show_cause' && k.responseDue && k.responseDue >= todayNpt() ? (
              <Checkbox
                label={`Decide before the reply deadline (${fmtAd(k.responseDue)})`}
                checked={decide.force}
                onChange={(e) => setDecide({ ...decide, force: e.currentTarget.checked })}
              />
            ) : null}
            <Group justify="flex-end">
              <Button size="xs" variant="default" onClick={() => setDecide(null)}>
                Cancel
              </Button>
              <Button
                size="xs"
                {...(stage?.effect === 'termination' ? { color: 'red' } : {})}
                loading={busy}
                disabled={!decide.note.trim()}
                onClick={async () => {
                  const ok = await run(
                    () =>
                      relations.decideCase(k.id, {
                        stageId: decide.stageId,
                        note: decide.note.trim(),
                        force: decide.force,
                        ...(decide.from ? { suspensionFrom: decide.from } : {}),
                        ...(decide.to ? { suspensionTo: decide.to } : {}),
                        ...(decide.lwd ? { lastWorkingDay: decide.lwd } : {}),
                      }),
                    'Decision recorded. The employee was told.',
                  );
                  if (ok) setDecide(null);
                }}
              >
                Record decision
              </Button>
            </Group>
          </Stack>
        </Paper>
      ) : null}
      {withdraw !== null ? (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <TextInput
              label="Why is the case withdrawn?"
              value={withdraw}
              onChange={(e) => setWithdraw(e.currentTarget.value)}
              maxLength={1000}
            />
            <Group justify="flex-end">
              <Button size="xs" variant="default" onClick={() => setWithdraw(null)}>
                Cancel
              </Button>
              <Button
                size="xs"
                loading={busy}
                disabled={!withdraw.trim()}
                onClick={async () => {
                  if (await run(() => relations.withdrawCase(k.id, withdraw.trim()), 'Case withdrawn.')) setWithdraw(null);
                }}
              >
                Withdraw case
              </Button>
            </Group>
          </Stack>
        </Paper>
      ) : null}
    </Stack>
  );
}

function StagesEditor({ opened, onClose }) {
  const q = useQuery(() => relations.stages(), [opened], { enabled: opened });
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false);
  const list = rows ?? q.data?.stages ?? [];
  const set = (i, patch) => setRows(list.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Modal
      opened={opened}
      onClose={() => {
        setRows(null);
        onClose();
      }}
      title="Disciplinary stages"
      size="xl"
      centered
    >
      <Stack>
        <Text size="sm" c="dimmed">
          The actions HR can decide, from least to most serious. Suspension asks for dates and goes on the career timeline; termination
          starts the exit.
        </Text>
        <Table.ScrollContainer minWidth={640}>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>Severity</Table.Th>
                <Table.Th>Effect</Table.Th>
                <Table.Th>Letter template code</Table.Th>
                <Table.Th>Active</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {list.map((s, i) => (
                <Table.Tr key={s.id || i}>
                  <Table.Td>
                    <TextInput size="xs" value={s.name} onChange={(e) => set(i, { name: e.currentTarget.value })} />
                  </Table.Td>
                  <Table.Td>
                    <NumberInput
                      size="xs"
                      w={70}
                      min={1}
                      max={9}
                      value={s.severity}
                      onChange={(v) => set(i, { severity: Number(v) || 1 })}
                    />
                  </Table.Td>
                  <Table.Td>
                    <Select
                      size="xs"
                      w={140}
                      value={s.effect}
                      allowDeselect={false}
                      onChange={(v) => set(i, { effect: v })}
                      data={[
                        { value: 'none', label: 'None' },
                        { value: 'suspension', label: 'Suspension' },
                        { value: 'termination', label: 'Termination' },
                      ]}
                    />
                  </Table.Td>
                  <Table.Td>
                    <TextInput
                      size="xs"
                      w={130}
                      value={s.letter_template_code ?? ''}
                      onChange={(e) => set(i, { letter_template_code: e.currentTarget.value || null })}
                    />
                  </Table.Td>
                  <Table.Td>
                    <Checkbox checked={s.active} onChange={(e) => set(i, { active: e.currentTarget.checked })} />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Group justify="space-between">
          <Button
            size="xs"
            variant="subtle"
            leftSection={<IconPlus size={14} />}
            onClick={() =>
              setRows([
                ...list,
                {
                  id: '',
                  code: `stage_${list.length + 1}`,
                  name: 'New stage',
                  severity: 3,
                  effect: 'none',
                  letter_template_code: null,
                  sort: list.length,
                  active: true,
                },
              ])
            }
          >
            Add stage
          </Button>
          <Button
            loading={busy}
            disabled={!rows}
            onClick={async () => {
              setBusy(true);
              try {
                await relations.saveStages(
                  list.map((s) => ({
                    ...(s.id ? { id: s.id } : {}),
                    code: s.code,
                    name: s.name.trim(),
                    severity: s.severity,
                    effect: s.effect,
                    letterTemplateCode: s.letter_template_code,
                    active: s.active,
                  })),
                );
                toast.ok('Stages saved.');
                setRows(null);
                await q.reload();
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

/** HR: disciplinary cases from incident to decision, with show-cause notices the employee answers in self-service. */
export default function DisciplinaryPage() {
  const q = useQuery(() => relations.cases(), []);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [openId, setOpenId] = useState(null);
  return (
    <Stack gap="lg">
      <PageHeader
        title="Disciplinary"
        subtitle="Open a case, ask the employee to explain, then decide. Cases are HR only; the employee sees a case once you send a notice or decide."
        actions={
          <>
            <Button variant="default" leftSection={<IconSettings size={16} stroke={1.6} />} onClick={() => setEditing(true)}>
              Stages
            </Button>
            <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setCreating(true)}>
              Open case
            </Button>
          </>
        }
      />
      <DataTable
        rows={q.data?.cases ?? []}
        rowKey={(r) => r.id}
        loading={q.loading}
        error={q.error}
        onRowClick={(r) => setOpenId(r.id)}
        emptyTitle="No disciplinary cases"
        emptyHint="Open a case when an incident needs a formal response."
        columns={[
          {
            key: 'n',
            header: 'Case',
            render: (r) => (
              <Text size="sm" ff="monospace">
                D-{r.number}
              </Text>
            ),
          },
          {
            key: 'e',
            header: 'Employee',
            render: (r) => (
              <>
                <Text size="sm">{r.employeeName}</Text>
                <Text size="xs" c="dimmed">
                  {r.empCode}
                </Text>
              </>
            ),
          },
          { key: 'c', header: 'Category', render: (r) => CASE_CATEGORY_LABEL[r.category] },
          { key: 'd', header: 'Incident', render: (r) => fmtAd(r.incidentDate) },
          { key: 's', header: 'Status', render: (r) => <CaseStatusBadge status={r.status} /> },
          { key: 'o', header: 'Outcome', render: (r) => r.stageName ?? (r.status === 'decided' ? 'No action' : '') },
        ]}
      />
      <OpenCase
        opened={creating}
        onClose={() => setCreating(false)}
        onCreated={(id) => {
          setCreating(false);
          void q.reload();
          setOpenId(id);
        }}
      />
      <StagesEditor opened={editing} onClose={() => setEditing(false)} />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Disciplinary case</Text>}>
        {openId ? (
          <CaseDetail
            id={openId}
            onChanged={() => {
              void q.reload();
            }}
          />
        ) : null}
      </Drawer>
    </Stack>
  );
}

export { OpenCase };
