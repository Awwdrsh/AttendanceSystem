import {
  Badge,
  Button,
  Drawer,
  Group,
  Modal,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import { IconLock, IconPlus, IconSearch } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { helpdesk, PRIORITY_LABEL } from '../../api/helpdesk';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, fmtTime, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { TicketBadges, TicketThread } from './TicketThread';

const ICON = { size: 16, stroke: 1.6 };

function Queue() {
  const [status, setStatus] = useState('active');
  const [assignee, setAssignee] = useState(null);
  const [categoryId, setCategoryId] = useState(null);
  const [search, setSearch] = useState('');
  const [overdue, setOverdue] = useState(false);
  const q = useQuery(
    () =>
      helpdesk.queue({
        status,
        ...(assignee ? { assignee } : {}),
        ...(categoryId ? { categoryId } : {}),
        ...(search.trim() ? { search: search.trim() } : {}),
        ...(overdue ? { breached: '1' } : {}),
      }),
    [status, assignee, categoryId, search, overdue],
  );
  const stats = useQuery(() => helpdesk.stats(), []);
  const cats = useQuery(() => helpdesk.categories(), []);
  const agents = useQuery(() => helpdesk.agents(), []);
  const [openId, setOpenId] = useState(null);
  const s = stats.data?.stats;
  const reload = () => {
    void q.reload();
    void stats.reload();
  };
  return (
    <Stack gap="md">
      {s ? (
        <StatStrip
          stats={[
            { key: 'o', label: 'Open', value: s.open, hint: `${s.unassigned} unassigned` },
            { key: 'd', label: 'Past due time', value: s.overdue, tone: s.overdue ? 'bad' : 'ok' },
            {
              key: 'r',
              label: 'Average time to resolve',
              value: s.avg_resolution_hours ? `${s.avg_resolution_hours} h` : '—',
              hint: 'Last 90 days',
            },
            { key: 's', label: 'Satisfaction', value: s.avg_rating ? `${s.avg_rating} / 5` : '—', hint: `${s.ratings} ratings` },
          ]}
        />
      ) : null}
      <Group gap="sm" wrap="wrap">
        <SegmentedControl
          value={status}
          onChange={setStatus}
          data={[
            { value: 'active', label: 'Open' },
            { value: 'resolved', label: 'Resolved' },
            { value: 'closed', label: 'Closed' },
          ]}
        />
        <Select
          placeholder="Anyone"
          clearable
          w={170}
          value={assignee}
          onChange={setAssignee}
          data={[
            { value: 'me', label: 'Assigned to me' },
            { value: 'none', label: 'Unassigned' },
            ...(agents.data?.agents ?? []).map((a) => ({ value: a.id, label: a.name })),
          ]}
        />
        <Select
          placeholder="All topics"
          clearable
          w={190}
          value={categoryId}
          onChange={setCategoryId}
          data={(cats.data?.categories ?? []).map((c) => ({ value: c.id, label: c.name }))}
        />
        <TextInput
          placeholder="Search subject, name or #"
          leftSection={<IconSearch {...ICON} />}
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
          w={230}
        />
        <Switch label="Past due only" checked={overdue} onChange={(e) => setOverdue(e.currentTarget.checked)} />
      </Group>
      <DataTable
        rows={q.data?.tickets ?? []}
        rowKey={(t) => t.id}
        loading={q.loading}
        error={q.error}
        onRowClick={(t) => setOpenId(t.id)}
        highlight={(t) => ['open', 'in_progress', 'waiting'].includes(t.status) && new Date(t.sla_due_at).getTime() < Date.now()}
        emptyTitle={status === 'active' ? 'No open tickets' : 'Nothing here'}
        emptyHint="Employees raise tickets from Help desk in their menu."
        columns={[
          {
            key: 'n',
            header: '#',
            render: (t) => (
              <Text size="sm" ff="monospace" c="dimmed">
                {t.number}
              </Text>
            ),
            width: 60,
          },
          {
            key: 'subject',
            header: 'Ticket',
            render: (t) => (
              <div>
                <Group gap={6} wrap="nowrap">
                  <Text size="sm" fw={500} truncate maw={320}>
                    {t.subject}
                  </Text>
                  {t.confidential ? <IconLock size={14} stroke={1.6} color="var(--mantine-color-grape-6)" /> : null}
                </Group>
                <Text size="xs" c="dimmed">
                  {t.category_name}
                </Text>
              </div>
            ),
          },
          {
            key: 'from',
            header: 'From',
            render: (t) => (
              <div>
                <Text size="sm">{t.employee_name ?? t.requester_name}</Text>
                {t.emp_code ? (
                  <Text size="xs" c="dimmed">
                    {t.emp_code}
                  </Text>
                ) : null}
              </div>
            ),
          },
          { key: 'st', header: 'Status', render: (t) => <TicketBadges status={t.status} priority={t.priority} /> },
          {
            key: 'who',
            header: 'Assigned',
            render: (t) => (
              <Text size="sm" c={t.assignee_name ? 'inherit' : 'dimmed'}>
                {t.assignee_name ?? 'Unassigned'}
              </Text>
            ),
          },
          {
            key: 'due',
            header: 'Due',
            render: (t) =>
              ['resolved', 'closed'].includes(t.status) ? (
                <Text size="xs" c="dimmed">
                  {t.resolved_at ? `Resolved ${relativeTime(t.resolved_at)}` : '—'}
                </Text>
              ) : (
                <Text size="xs" c={new Date(t.sla_due_at).getTime() < Date.now() ? 'red' : 'dimmed'}>
                  {fmtDateShort(t.sla_due_at)}, {fmtTime(t.sla_due_at)}
                </Text>
              ),
          },
        ]}
      />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="xl" title={<Text fw={600}>Ticket</Text>}>
        {openId ? <TicketThread ticketId={openId} agents={agents.data?.agents ?? []} onChanged={reload} /> : null}
      </Drawer>
    </Stack>
  );
}

function Setup() {
  const cats = useQuery(() => helpdesk.categories(), []);
  const agents = useQuery(() => helpdesk.agents(), []);
  const pol = useQuery(() => helpdesk.policy(), []);
  const [p, setP] = useState(null);
  const [edit, setEdit] = useState(null);
  useEffect(() => {
    if (pol.data) setP(pol.data.policy);
  }, [pol.data]);
  const savePolicy = async () => {
    if (!p) return;
    try {
      await helpdesk.savePolicy(p);
      toast.ok('Settings saved.');
    } catch (e) {
      toast.err(e);
    }
  };
  const saveCat = async () => {
    if (!edit) return;
    const body = {
      name: edit.name.trim(),
      description: edit.description ?? null,
      confidential: !!edit.confidential,
      ownerUserId: edit.owner_user_id ?? null,
      sort: edit.sort ?? 0,
      active: edit.active !== false,
    };
    try {
      if (edit.id) await helpdesk.updateCategory(edit.id, body);
      else await helpdesk.createCategory(body);
      toast.ok('Topic saved.');
      setEdit(null);
      await cats.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Stack gap="xl">
      <div>
        <SectionTitle
          hint="Each topic can have an owner who answers its tickets. Confidential topics are seen only by HR, never by managers."
          actions={
            <Button size="sm" leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ name: '', active: true, confidential: false })}>
              New topic
            </Button>
          }
        >
          Topics
        </SectionTitle>
        <DataTable
          rows={cats.data?.categories ?? []}
          rowKey={(c) => c.id}
          loading={cats.loading}
          error={cats.error}
          onRowClick={(c) => setEdit(c)}
          columns={[
            {
              key: 'n',
              header: 'Topic',
              render: (c) => (
                <div>
                  <Group gap={6}>
                    <Text size="sm" fw={500}>
                      {c.name}
                    </Text>
                    {c.confidential ? (
                      <Badge size="xs" color="grape" variant="light">
                        Confidential
                      </Badge>
                    ) : null}
                    {!c.active ? (
                      <Badge size="xs" color="gray" variant="light">
                        Off
                      </Badge>
                    ) : null}
                  </Group>
                  <Text size="xs" c="dimmed">
                    {c.description}
                  </Text>
                </div>
              ),
            },
            {
              key: 'o',
              header: 'Owner',
              render: (c) => (
                <Text size="sm" c={c.owner_name ? 'inherit' : 'dimmed'}>
                  {c.owner_name ?? 'HR team'}
                </Text>
              ),
            },
          ]}
        />
      </div>
      {p ? (
        <Paper withBorder p="lg" maw={640}>
          <SectionTitle hint="Hours from raising a ticket to resolving it. Tickets past their due time are escalated to HR.">
            Response times
          </SectionTitle>
          <SimpleGrid cols={{ base: 2, sm: 4 }}>
            {Object.keys(PRIORITY_LABEL).map((k) => (
              <NumberInput
                key={k}
                label={PRIORITY_LABEL[k]}
                suffix=" h"
                min={1}
                max={720}
                value={p.slaHours[k]}
                onChange={(v) => setP({ ...p, slaHours: { ...p.slaHours, [k]: Number(v) || 1 } })}
              />
            ))}
          </SimpleGrid>
          <SimpleGrid cols={{ base: 1, sm: 2 }} mt="md">
            <NumberInput
              label="Close resolved tickets after (days)"
              min={1}
              max={60}
              value={p.autoCloseDays}
              onChange={(v) => setP({ ...p, autoCloseDays: Number(v) || 1 })}
            />
            <NumberInput
              label="Requester can reopen within (days)"
              min={0}
              max={60}
              value={p.reopenDays}
              onChange={(v) => setP({ ...p, reopenDays: Number(v) || 0 })}
            />
          </SimpleGrid>
          <Group justify="flex-end" mt="md">
            <Button onClick={savePolicy}>Save</Button>
          </Group>
        </Paper>
      ) : null}
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? 'Edit topic' : 'New topic'} centered>
        {edit ? (
          <Stack>
            <TextInput label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.currentTarget.value })} required />
            <TextInput
              label="Description"
              value={edit.description ?? ''}
              onChange={(e) => setEdit({ ...edit, description: e.currentTarget.value })}
            />
            <Switch
              label="Confidential (HR only)"
              checked={!!edit.confidential}
              onChange={(e) => setEdit({ ...edit, confidential: e.currentTarget.checked })}
            />
            <Select
              label="Owner"
              description="Leave empty for the whole HR team."
              clearable
              searchable
              value={edit.owner_user_id ?? null}
              onChange={(v) => setEdit({ ...edit, owner_user_id: v })}
              data={(agents.data?.agents ?? []).filter((a) => !edit.confidential || a.is_hr).map((a) => ({ value: a.id, label: a.name }))}
            />
            <Switch
              label="In use"
              checked={edit.active !== false}
              onChange={(e) => setEdit({ ...edit, active: e.currentTarget.checked })}
            />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEdit(null)}>
                Cancel
              </Button>
              <Button disabled={!edit.name.trim()} onClick={saveCat}>
                Save topic
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}

export default function HelpdeskPage() {
  const [tab, setTab] = useState('queue');
  return (
    <Stack gap="md">
      <PageHeader title="Help desk" subtitle="Questions and requests from employees, with due times by priority." />
      <SegmentedControl
        value={tab}
        onChange={setTab}
        style={{ alignSelf: 'flex-start' }}
        data={[
          { value: 'queue', label: 'Tickets' },
          { value: 'setup', label: 'Topics & response times' },
        ]}
      />
      {tab === 'queue' ? <Queue /> : <Setup />}
    </Stack>
  );
}
