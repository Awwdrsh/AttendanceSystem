import { ActionIcon, Avatar, Badge, Button, Checkbox, Group, Modal, MultiSelect, Paper, ScrollArea, SegmentedControl, Select, Stack, Switch, Text, TextInput, Tooltip } from '@mantine/core';
import { IconCalendar, IconChecklist, IconMessage, IconPaperclip, IconPlus, IconSettings, IconTrash } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PRIORITY_COLOR, PRIORITY_LABEL, tasks as tasksApi, type Board, type Task, type TaskColumn } from '../../api/tasks';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { TaskDrawer } from './TaskDrawer';

const initials = (name: string) => name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();

function TaskCard({ t, onOpen, onDragStart }: { t: Task; onOpen: () => void; onDragStart: (e: React.DragEvent) => void }) {
  const today = todayNpt();
  const overdue = !t.completed_at && !!t.due_date && t.due_date < today;
  return (
    <Paper withBorder radius="md" p="sm" draggable onDragStart={onDragStart} onClick={onOpen} style={{ cursor: 'pointer' }} role="button" aria-label={t.title}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(); }} tabIndex={0}>
      <Stack gap={6}>
        <Text size="sm" fw={500} {...(t.completed_at ? { td: 'line-through', c: 'dimmed' } : {})}>{t.title}</Text>
        {t.labels.length ? <Group gap={4}>{t.labels.map((l) => <Badge key={l} size="xs" variant="light" color="gray">{l}</Badge>)}</Group> : null}
        <Group justify="space-between" wrap="nowrap">
          <Group gap={8} wrap="nowrap">
            {t.priority !== 'normal' ? <Badge size="xs" variant="light" color={PRIORITY_COLOR[t.priority]}>{PRIORITY_LABEL[t.priority]}</Badge> : null}
            {t.due_date ? <Group gap={3} wrap="nowrap"><IconCalendar size={12} stroke={1.6} {...(overdue ? { color: 'var(--hj-bad)' } : {})} />
              <Text size="xs" c={overdue ? 'red' : 'dimmed'}>{fmtDateShort(t.due_date)}</Text></Group> : null}
            {t.checklist_total ? <Group gap={3} wrap="nowrap"><IconChecklist size={12} stroke={1.6} /><Text size="xs" c="dimmed">{t.checklist_done}/{t.checklist_total}</Text></Group> : null}
            {t.comments ? <Group gap={3} wrap="nowrap"><IconMessage size={12} stroke={1.6} /><Text size="xs" c="dimmed">{t.comments}</Text></Group> : null}
            {t.attachments ? <IconPaperclip size={12} stroke={1.6} /> : null}
          </Group>
          <Avatar.Group spacing={6}>
            {t.assignees.slice(0, 3).map((a) => <Tooltip key={a.userId} label={a.name}><Avatar size={22} radius="xl" color="brand">{initials(a.name)}</Avatar></Tooltip>)}
          </Avatar.Group>
        </Group>
      </Stack>
    </Paper>
  );
}

function BoardSettings({ board, members, onClose, onSaved }: { board: Board; members: Array<{ user_id: string; name: string }>; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(board.name);
  const [cols, setCols] = useState<TaskColumn[]>(board.columns);
  const [moveTo, setMoveTo] = useState<string | null>(null);
  const [memberIds, setMemberIds] = useState(members.map((m) => m.user_id));
  const people = useQuery(() => tasksApi.people(), [], { enabled: board.kind === 'team' });
  const [busy, setBusy] = useState(false);
  const removed = board.columns.filter((c) => !cols.some((x) => x.id === c.id));
  const save = async () => {
    setBusy(true);
    try {
      await tasksApi.updateBoard(board.id, { name, columns: cols, ...(moveTo ? { moveTo } : {}) });
      if (board.kind === 'team') await tasksApi.setMembers(board.id, memberIds);
      toast.ok('Board saved.'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const options = [...(people.data?.people ?? []).map((p) => ({ value: p.user_id, label: p.name })), ...members.filter((m) => !(people.data?.people ?? []).some((p) => p.user_id === m.user_id)).map((m) => ({ value: m.user_id, label: m.name }))];
  return (
    <Modal opened onClose={onClose} centered size="lg" title={<Text fw={600}>Board settings</Text>}>
      <Stack>
        <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} />
        <div>
          <Text size="sm" fw={500} mb={4}>Columns</Text>
          <Stack gap={6}>
            {cols.map((c, i) => (
              <Group key={c.id} gap="xs" wrap="nowrap">
                <TextInput style={{ flex: 1 }} value={c.name} onChange={(e) => { const v = e.currentTarget.value; setCols((x) => x.map((y, j) => (j === i ? { ...y, name: v } : y))); }} />
                <Checkbox label="Done" checked={c.done} onChange={(e) => { const v = e.currentTarget.checked; setCols((x) => x.map((y, j) => (j === i ? { ...y, done: v } : y))); }} />
                <ActionIcon variant="subtle" color="gray" aria-label={`Remove ${c.name}`} disabled={cols.length <= 1} onClick={() => setCols((x) => x.filter((_, j) => j !== i))}><IconTrash size={16} stroke={1.6} /></ActionIcon>
              </Group>
            ))}
            {cols.length < 8 ? <Button variant="subtle" size="xs" w="fit-content" leftSection={<IconPlus size={14} />}
              onClick={() => setCols((x) => [...x, { id: `col-${Date.now().toString(36).slice(-6)}`, name: 'New column', done: false }])}>Add column</Button> : null}
          </Stack>
          {removed.length ? <Select mt="sm" label="Move tasks from removed columns to" data={cols.map((c) => ({ value: c.id, label: c.name }))} value={moveTo} onChange={setMoveTo} /> : null}
        </div>
        {board.kind === 'team' ? (
          <MultiSelect label="People on this board" description="They can see and work on its tasks" searchable data={options} value={memberIds} onChange={setMemberIds} />
        ) : null}
        <Group justify="space-between">
          {board.kind === 'team' ? <Button variant="subtle" color="red" onClick={async () => { try { await tasksApi.updateBoard(board.id, { archived: true }); toast.ok('Board archived.'); onSaved(); onClose(); } catch (e) { toast.err(e); } }}>Archive board</Button> : <span />}
          <Group gap="xs"><Button variant="default" onClick={onClose}>Cancel</Button><Button loading={busy} disabled={!cols.some((c) => c.done) || !name.trim()} onClick={save}>Save</Button></Group>
        </Group>
      </Stack>
    </Modal>
  );
}

function BoardReport({ boardId }: { boardId: string }) {
  const q = useQuery(() => tasksApi.report(boardId), [boardId]);
  const d = q.data;
  return (
    <Stack gap="lg">
      <StatStrip stats={[
        { key: 'open', label: 'Open', value: d?.totals.open ?? 0 },
        { key: 'overdue', label: 'Overdue', value: d?.totals.overdue ?? 0, ...(d?.totals.overdue ? { tone: 'bad' as const } : {}) },
        { key: 'week', label: 'Done this week', value: d?.totals.done_this_week ?? 0 },
        { key: 'un', label: 'Unassigned', value: d?.totals.unassigned ?? 0 },
      ]} />
      <Group gap="xs">{(d?.columns ?? []).map((c) => <Badge key={c.id} variant="light" size="lg" color={c.done ? 'teal' : 'gray'}>{c.name}: {c.tasks}</Badge>)}</Group>
      <div>
        <SectionTitle>By person</SectionTitle>
        <DataTable rows={d?.assignees ?? []} rowKey={(a) => a.user_id} loading={q.loading} error={q.error} emptyTitle="No assigned tasks"
          columns={[
            { key: 'n', header: 'Name', render: (a) => a.name },
            { key: 'o', header: 'Open', align: 'right', render: (a) => <span className="num">{a.open}</span> },
            { key: 'od', header: 'Overdue', align: 'right', render: (a) => <Text size="sm" className="num" {...(a.overdue ? { c: 'red' } : {})}>{a.overdue}</Text> },
            { key: 'd', header: 'Done', align: 'right', render: (a) => <span className="num">{a.done}</span> },
          ]} />
      </div>
      <div>
        <SectionTitle>Overdue tasks</SectionTitle>
        <DataTable rows={d?.overdueTasks ?? []} rowKey={(t) => t.id} emptyTitle="Nothing overdue"
          columns={[
            { key: 't', header: 'Task', render: (t) => t.title },
            { key: 'due', header: 'Due', render: (t) => fmtDateShort(t.due_date) },
            { key: 'who', header: 'Assigned to', render: (t) => t.assignees.map((a) => a.name).join(', ') || '—' },
          ]} />
      </div>
    </Stack>
  );
}

/** Personal and team task boards (study: Tasks): columns, drag-and-drop, checklists, comments, files, and a board report. */
export default function TasksPage() {
  const { boardId } = useParams();
  const nav = useNavigate();
  const boards = useQuery(() => tasksApi.boards(), []);
  const list = boards.data?.boards ?? [];
  const currentId = boardId ?? list.find((b) => b.kind === 'personal')?.id ?? list[0]?.id ?? null;
  const board = useQuery(() => tasksApi.board(currentId!), [currentId], { enabled: !!currentId });
  const [view, setView] = useState<'board' | 'report'>('board');
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null); const [newTitle, setNewTitle] = useState('');
  const [settings, setSettings] = useState(false);
  const [creating, setCreating] = useState(false); const [newName, setNewName] = useState(''); const [withReports, setWithReports] = useState(true);
  const [dragId, setDragId] = useState<string | null>(null);
  useEffect(() => { setView('board'); }, [currentId]);

  const b = board.data;
  const byColumn = useMemo(() => {
    const m = new Map<string, Task[]>();
    for (const c of b?.board.columns ?? []) m.set(c.id, []);
    for (const t of b?.tasks ?? []) m.get(t.column_id)?.push(t);
    return m;
  }, [b]);
  const reload = async () => { await Promise.all([board.reload(), boards.reload()]); };
  const add = async (columnId: string) => {
    const title = newTitle.trim();
    if (!title || !currentId) return;
    try { await tasksApi.create(currentId, { title, columnId }); setNewTitle(''); setAdding(null); await reload(); } catch (e) { toast.err(e); }
  };
  const drop = async (columnId: string) => {
    const id = dragId; setDragId(null);
    const t = b?.tasks.find((x) => x.id === id);
    if (!t || t.column_id === columnId) return;
    board.setData(b ? { ...b, tasks: b.tasks.map((x) => (x.id === id ? { ...x, column_id: columnId } : x)) } : b);
    try { await tasksApi.update(t.id, { columnId }); await reload(); } catch (e) { toast.err(e); await board.reload(); }
  };

  return (
    <Stack gap="lg">
      <PageHeader title="Tasks" subtitle="Your own to-dos, and the boards you share with your team."
        actions={<Group gap="xs">
          {list.length ? <Select w={240} allowDeselect={false} value={currentId} onChange={(v) => v && nav(`/tasks/${v}`)}
            data={list.map((x) => ({ value: x.id, label: `${x.name}${x.kind === 'personal' ? ' (just you)' : ''}${x.open ? ` · ${x.open} open` : ''}` }))} /> : null}
          {boards.data?.canCreateTeamBoard ? <Button variant="default" leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => { setCreating(true); setNewName(''); }}>Team board</Button> : null}
          {b && b.access === 'owner' ? <ActionIcon variant="default" size="lg" aria-label="Board settings" onClick={() => setSettings(true)}><IconSettings size={16} stroke={1.6} /></ActionIcon> : null}
        </Group>} />

      {b ? (
        <>
          <Group justify="space-between">
            <Text size="sm" c="dimmed">{b.board.kind === 'personal' ? 'Only you can see this board.' : `Owned by ${b.ownerName ?? 'someone'}; ${b.members.length} other ${b.members.length === 1 ? 'person' : 'people'} on it.`}</Text>
            <SegmentedControl size="xs" value={view} onChange={(v) => setView(v as 'board' | 'report')} data={[{ value: 'board', label: 'Board' }, { value: 'report', label: 'Report' }]} />
          </Group>
          {view === 'report' ? <BoardReport boardId={b.board.id} /> : (
            <ScrollArea type="auto" offsetScrollbars>
              <Group align="flex-start" wrap="nowrap" gap="md" pb="sm">
                {b.board.columns.map((c) => {
                  const items = byColumn.get(c.id) ?? [];
                  return (
                    <Paper key={c.id} radius="lg" p="sm" w={290} miw={260} style={{ background: 'var(--hj-canvas)', flexShrink: 0 }}
                      onDragOver={(e) => e.preventDefault()} onDrop={() => void drop(c.id)}>
                      <Group justify="space-between" mb="sm">
                        <Group gap={6}><Text fw={600} size="sm">{c.name}</Text><Badge size="sm" variant="light" color={c.done ? 'teal' : 'gray'}>{items.length}</Badge></Group>
                        <ActionIcon variant="subtle" size="sm" aria-label={`Add a task to ${c.name}`} onClick={() => { setAdding(c.id); setNewTitle(''); }}><IconPlus size={14} /></ActionIcon>
                      </Group>
                      <Stack gap="xs">
                        {adding === c.id ? (
                          <TextInput autoFocus placeholder="Task title, then Enter" value={newTitle} onChange={(e) => setNewTitle(e.currentTarget.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') void add(c.id); if (e.key === 'Escape') setAdding(null); }} onBlur={() => !newTitle && setAdding(null)} />
                        ) : null}
                        {items.map((t) => <TaskCard key={t.id} t={t} onOpen={() => setOpen(t.id)} onDragStart={() => setDragId(t.id)} />)}
                        {!items.length && adding !== c.id ? <Text size="xs" c="dimmed" ta="center" py="md">Drop tasks here</Text> : null}
                      </Stack>
                    </Paper>
                  );
                })}
              </Group>
            </ScrollArea>
          )}
        </>
      ) : boards.loading || board.loading ? null : <EmptyState title="No boards yet" hint="Your personal board appears here as soon as it loads." />}

      <TaskDrawer taskId={open} onClose={() => setOpen(null)} onChanged={() => void reload()} />
      {settings && b ? <BoardSettings board={b.board} members={b.members} onClose={() => setSettings(false)} onSaved={() => void reload()} /> : null}
      <Modal opened={creating} onClose={() => setCreating(false)} centered title={<Text fw={600}>New team board</Text>}>
        <Stack>
          <TextInput label="Name" placeholder="For example: Store opening" value={newName} onChange={(e) => setNewName(e.currentTarget.value)} data-autofocus />
          <Switch label="Add the people who report to me" checked={withReports} onChange={(e) => setWithReports(e.currentTarget.checked)} />
          <Text size="xs" c="dimmed">You can add or remove people later in the board settings.</Text>
          <Group justify="flex-end"><Button variant="default" onClick={() => setCreating(false)}>Cancel</Button>
            <Button disabled={!newName.trim()} onClick={async () => {
              try { const r = await tasksApi.createBoard({ name: newName.trim(), addMyReports: withReports }); setCreating(false); await boards.reload(); nav(`/tasks/${r.board.id}`); } catch (e) { toast.err(e); }
            }}>Create board</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
