import {
  ActionIcon,
  Badge,
  Button,
  Drawer,
  Group,
  NumberInput,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  Textarea,
} from '@mantine/core';
import { IconArrowDown, IconArrowUp, IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { auth as authApi } from '../../api';
import { ASSIGNEE_LABEL, lifecycle } from '../../api/lifecycle';
import { DataTable } from '../../components/DataTable';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };
function offsetText(n, kind) {
  const anchor = kind === 'onboarding' ? 'joining' : 'the last day';
  if (n === 0) return kind === 'onboarding' ? 'On the first day' : 'On the last day';
  return `${Math.abs(n)} ${Math.abs(n) === 1 ? 'day' : 'days'} ${n < 0 ? 'before' : 'after'} ${anchor}`;
}

/** Checklist templates for onboarding or exit clearance: tasks, owners and due days relative to the join or last working day. */
export function TemplatesPanel({ kind }) {
  const q = useQuery(() => lifecycle.templates(kind), [kind]);
  const users = useQuery(() => authApi.users(), []);
  const [edit, setEdit] = useState(null);
  const [busy, setBusy] = useState(false);
  const open = (t) =>
    setEdit(
      t
        ? {
            id: t.id,
            kind,
            name: t.name,
            description: t.description,
            branchId: t.branch_id,
            orgUnitId: t.org_unit_id,
            isDefault: t.is_default,
            active: t.active,
            tasks: t.tasks.map((x) => ({
              title: x.title,
              description: x.description ?? null,
              department: x.department ?? null,
              assigneeType: x.assignee_type ?? 'hr',
              assigneeUserId: x.assignee_user_id ?? null,
              dueOffsetDays: x.due_offset_days ?? 0,
              link: x.link ?? null,
            })),
          }
        : { kind, name: '', description: '', isDefault: false, active: true, tasks: [{ title: '', assigneeType: 'hr', dueOffsetDays: 0 }] },
    );
  const setTask = (i, patch) => setEdit((e) => e && { ...e, tasks: e.tasks.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const move = (i, d) =>
    setEdit((e) => {
      if (!e) return e;
      const tasks = [...e.tasks];
      const j = i + d;
      if (j < 0 || j >= tasks.length) return e;
      [tasks[i], tasks[j]] = [tasks[j], tasks[i]];
      return { ...e, tasks };
    });
  const save = async () => {
    if (!edit) return;
    setBusy(true);
    try {
      const { id, ...body } = edit;
      const clean = {
        ...body,
        tasks: body.tasks
          .filter((t) => t.title.trim())
          .map((t) => ({ ...t, link: t.link?.trim() || null, department: t.department?.trim() || null })),
      };
      if (id) await lifecycle.updateTemplate(id, clean);
      else await lifecycle.createTemplate(clean);
      toast.ok('Checklist saved.');
      setEdit(null);
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const userOptions = (users.data?.users ?? []).map((u) => ({ value: u.id, label: u.name }));
  return (
    <Stack gap="md">
      <SectionTitle
        hint={
          kind === 'onboarding'
            ? 'Every new joiner gets these tasks. The most specific checklist (department, then branch, then default) is used.'
            : 'Tasks each department signs off before someone leaves.'
        }
        actions={
          <Button size="sm" leftSection={<IconPlus {...ICON} />} onClick={() => open()}>
            New checklist
          </Button>
        }
      >
        {kind === 'onboarding' ? 'Onboarding checklists' : 'Exit clearance checklists'}
      </SectionTitle>
      <DataTable
        rows={q.data?.templates ?? []}
        rowKey={(t) => t.id}
        loading={q.loading}
        error={q.error}
        onRowClick={open}
        emptyTitle="No checklists yet"
        emptyHint="Create a checklist so every person gets the same steps."
        columns={[
          {
            key: 'name',
            header: 'Checklist',
            render: (t) => (
              <Group gap={6}>
                <Text size="sm" fw={500}>
                  {t.name}
                </Text>
                {t.is_default ? (
                  <Badge size="xs" variant="light">
                    Default
                  </Badge>
                ) : null}
                {!t.active ? (
                  <Badge size="xs" color="gray" variant="light">
                    Off
                  </Badge>
                ) : null}
              </Group>
            ),
          },
          {
            key: 'scope',
            header: 'Used for',
            render: (t) => (
              <Text size="sm" c="dimmed">
                {[t.org_unit_name, t.branch_name].filter(Boolean).join(', ') || 'Everyone'}
              </Text>
            ),
          },
          { key: 'tasks', header: 'Tasks', align: 'right', render: (t) => <span className="num">{t.tasks.length}</span> },
        ]}
      />
      <Drawer
        opened={!!edit}
        onClose={() => setEdit(null)}
        position="right"
        size="xl"
        title={<Text fw={600}>{edit?.id ? 'Edit checklist' : 'New checklist'}</Text>}
      >
        {edit ? (
          <Stack gap="md">
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <TextInput label="Name" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.currentTarget.value })} required />
              <Group gap="lg" mt={{ sm: 28 }}>
                <Switch
                  label="Default"
                  checked={!!edit.isDefault}
                  onChange={(e) => setEdit({ ...edit, isDefault: e.currentTarget.checked })}
                />
                <Switch
                  label="In use"
                  checked={edit.active !== false}
                  onChange={(e) => setEdit({ ...edit, active: e.currentTarget.checked })}
                />
              </Group>
            </SimpleGrid>
            <Textarea
              label="Description"
              value={edit.description ?? ''}
              onChange={(e) => setEdit({ ...edit, description: e.currentTarget.value })}
              autosize
              minRows={1}
            />
            <SectionTitle hint="Days count from the join date (onboarding) or the last working day (exit). Use a minus sign for days before.">
              Tasks
            </SectionTitle>
            {edit.tasks.map((t, i) => (
              <Paper key={i} withBorder p="sm">
                <Stack gap="xs">
                  <Group gap="xs" wrap="nowrap" align="flex-end">
                    <TextInput
                      label={`Task ${i + 1}`}
                      value={t.title}
                      onChange={(e) => setTask(i, { title: e.currentTarget.value })}
                      style={{ flex: 1 }}
                    />
                    <ActionIcon variant="subtle" color="gray" onClick={() => move(i, -1)} aria-label="Move up" disabled={i === 0}>
                      <IconArrowUp {...ICON} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="gray"
                      onClick={() => move(i, 1)}
                      aria-label="Move down"
                      disabled={i === edit.tasks.length - 1}
                    >
                      <IconArrowDown {...ICON} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      onClick={() => setEdit({ ...edit, tasks: edit.tasks.filter((_, j) => j !== i) })}
                      aria-label="Remove task"
                    >
                      <IconTrash {...ICON} />
                    </ActionIcon>
                  </Group>
                  <SimpleGrid cols={{ base: 1, sm: 3 }}>
                    <Select
                      label="Who does it"
                      value={t.assigneeType}
                      allowDeselect={false}
                      onChange={(v) => v && setTask(i, { assigneeType: v })}
                      data={Object.keys(ASSIGNEE_LABEL).map((k) => ({ value: k, label: ASSIGNEE_LABEL[k] }))}
                    />
                    {t.assigneeType === 'user' ? (
                      <Select
                        label="Person"
                        searchable
                        value={t.assigneeUserId ?? null}
                        onChange={(v) => setTask(i, { assigneeUserId: v })}
                        data={userOptions}
                      />
                    ) : (
                      <TextInput
                        label="Department"
                        placeholder="IT, Finance, Admin"
                        value={t.department ?? ''}
                        onChange={(e) => setTask(i, { department: e.currentTarget.value })}
                      />
                    )}
                    <NumberInput
                      label="Due (days)"
                      value={t.dueOffsetDays}
                      min={-90}
                      max={365}
                      onChange={(v) => setTask(i, { dueOffsetDays: Number(v) || 0 })}
                      description={offsetText(t.dueOffsetDays, kind)}
                    />
                  </SimpleGrid>
                  <SimpleGrid cols={{ base: 1, sm: 2 }}>
                    <TextInput
                      label="Details"
                      value={t.description ?? ''}
                      onChange={(e) => setTask(i, { description: e.currentTarget.value })}
                    />
                    <TextInput
                      label="Link to a page"
                      placeholder="/me/profile"
                      value={t.link ?? ''}
                      onChange={(e) => setTask(i, { link: e.currentTarget.value })}
                    />
                  </SimpleGrid>
                </Stack>
              </Paper>
            ))}
            <Button
              variant="subtle"
              leftSection={<IconPlus {...ICON} />}
              style={{ alignSelf: 'flex-start' }}
              onClick={() => setEdit({ ...edit, tasks: [...edit.tasks, { title: '', assigneeType: 'hr', dueOffsetDays: 0 }] })}
            >
              Add task
            </Button>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEdit(null)}>
                Cancel
              </Button>
              <Button loading={busy} disabled={!edit.name.trim()} onClick={save}>
                Save checklist
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}
