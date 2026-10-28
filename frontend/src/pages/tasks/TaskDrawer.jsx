import {
  ActionIcon,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Divider,
  Drawer,
  Group,
  MultiSelect,
  Select,
  Stack,
  TagsInput,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconPaperclip, IconTrash, IconX } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { PRIORITY_LABEL, tasks as tasksApi } from '../../api/tasks';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { FileUpload } from '../../components/FileUpload';
import { fmtDateShort, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** One task: edit its details, move it, tick its checklist, comment and attach files. */
export function TaskDrawer({ taskId, onClose, onChanged }) {
  const { user } = useAuth();
  const q = useQuery(() => tasksApi.get(taskId), [taskId], { enabled: !!taskId });
  const d = q.data;
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [newItem, setNewItem] = useState('');
  const [comment, setComment] = useState('');
  useEffect(() => {
    if (d) {
      setTitle(d.task.title);
      setDesc(d.task.description ?? '');
    }
  }, [d?.task.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const update = async (b) => {
    if (!taskId) return;
    try {
      await tasksApi.update(taskId, b);
      await q.reload();
      onChanged();
    } catch (e) {
      toast.err(e);
    }
  };
  const run = async (fn) => {
    try {
      await fn();
      await q.reload();
      onChanged();
    } catch (e) {
      toast.err(e);
    }
  };
  const memberOptions = [...(d?.members ?? []).map((m) => ({ value: m.user_id, label: m.name }))];
  if (d && d.board.kind === 'team' && user && !memberOptions.some((m) => m.value === user.id))
    memberOptions.unshift({ value: user.id, label: user.name });
  for (const a of d?.task.assignees ?? [])
    if (!memberOptions.some((m) => m.value === a.userId)) memberOptions.push({ value: a.userId, label: a.name });

  return (
    <Drawer opened={!!taskId} onClose={onClose} position="right" size="lg" title={<Text fw={600}>{d?.board.name ?? 'Task'}</Text>}>
      {d ? (
        <Stack gap="md">
          <TextInput
            label="Title"
            value={title}
            onChange={(e) => setTitle(e.currentTarget.value)}
            onBlur={() => title.trim() && title !== d.task.title && void update({ title: title.trim() })}
          />
          <Group grow align="flex-start">
            <Select
              label="Column"
              allowDeselect={false}
              value={d.task.column_id}
              data={d.board.columns.map((c) => ({ value: c.id, label: c.name }))}
              onChange={(v) => v && void update({ columnId: v })}
            />
            <Select
              label="Priority"
              allowDeselect={false}
              value={d.task.priority}
              data={Object.entries(PRIORITY_LABEL).map(([value, label]) => ({ value, label }))}
              onChange={(v) => v && void update({ priority: v })}
            />
          </Group>
          <Group grow align="flex-start">
            <DateField label="Due" value={d.task.due_date} onChange={(v) => void update({ dueDate: v })} />
            {d.board.kind === 'team' ? (
              <MultiSelect
                label="Assigned to"
                data={memberOptions}
                value={d.task.assignees.map((a) => a.userId)}
                onChange={(v) => void update({ assigneeIds: v })}
                searchable
                placeholder="Nobody"
              />
            ) : (
              <TextInput label="Assigned to" value="You" readOnly />
            )}
          </Group>
          <TagsInput
            label="Labels"
            value={d.task.labels}
            maxTags={8}
            onChange={(v) => void update({ labels: v.map((x) => x.slice(0, 24)) })}
            placeholder="Add a label and press Enter"
          />
          <Textarea
            label="Description"
            autosize
            minRows={3}
            value={desc}
            onChange={(e) => setDesc(e.currentTarget.value)}
            onBlur={() => desc !== (d.task.description ?? '') && void update({ description: desc || null })}
          />

          <Divider
            label={`Checklist${d.checklist.length ? ` (${d.checklist.filter((c) => c.done).length}/${d.checklist.length})` : ''}`}
            labelPosition="left"
          />
          <Stack gap={6}>
            {d.checklist.map((c) => (
              <Group key={c.id} justify="space-between" wrap="nowrap">
                <Checkbox
                  label={c.text}
                  checked={c.done}
                  onChange={(e) => void run(() => tasksApi.updateChecklist(c.id, { done: e.currentTarget.checked }))}
                />
                <ActionIcon
                  variant="subtle"
                  color="gray"
                  size="sm"
                  aria-label="Remove item"
                  onClick={() => void run(() => tasksApi.removeChecklist(c.id))}
                >
                  <IconX size={14} />
                </ActionIcon>
              </Group>
            ))}
            <TextInput
              placeholder="Add an item and press Enter"
              value={newItem}
              onChange={(e) => setNewItem(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newItem.trim()) {
                  const t = newItem.trim();
                  setNewItem('');
                  void run(() => tasksApi.addChecklist(d.task.id, t));
                }
              }}
            />
          </Stack>

          <Divider label="Attachments" labelPosition="left" />
          <Stack gap={6}>
            {d.attachments.map((a) => (
              <Group key={a.id} justify="space-between" wrap="nowrap">
                <Group gap={6} wrap="nowrap">
                  <IconPaperclip size={14} stroke={1.6} />
                  <Anchor size="sm" href={tasksApi.attachmentUrl(a.id)} target="_blank" rel="noopener noreferrer">
                    {a.filename}
                  </Anchor>
                  <Text size="xs" c="dimmed">
                    {a.added_by_name}
                  </Text>
                </Group>
                <ActionIcon
                  variant="subtle"
                  color="gray"
                  size="sm"
                  aria-label="Remove attachment"
                  onClick={() => void run(() => tasksApi.removeAttachment(a.id))}
                >
                  <IconX size={14} />
                </ActionIcon>
              </Group>
            ))}
            <FileUpload
              purpose="task_attachment"
              value={null}
              label="Attach a file"
              onChange={(f) => {
                if (f) void run(() => tasksApi.attach(d.task.id, f.id));
              }}
            />
          </Stack>

          <Divider label="Comments" labelPosition="left" />
          <Stack gap="xs">
            {d.comments.map((c) => (
              <div key={c.id}>
                <Group gap={6}>
                  <Text size="sm" fw={600}>
                    {c.name}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {relativeTime(c.created_at)}
                  </Text>
                </Group>
                <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                  {c.body}
                </Text>
              </div>
            ))}
            <Textarea
              placeholder="Write a comment"
              autosize
              minRows={2}
              value={comment}
              onChange={(e) => setComment(e.currentTarget.value)}
            />
            <Group justify="flex-end">
              <Button
                size="xs"
                disabled={!comment.trim()}
                onClick={() => {
                  const b = comment.trim();
                  setComment('');
                  void run(() => tasksApi.comment(d.task.id, b));
                }}
              >
                Comment
              </Button>
            </Group>
          </Stack>

          <Divider />
          <Group justify="space-between">
            <Text size="xs" c="dimmed">
              Created {fmtDateShort(d.task.created_at.slice(0, 10))}
              {d.task.completed_at ? `, done ${relativeTime(d.task.completed_at)}` : ''}
            </Text>
            {d.access === 'owner' || d.task.created_by === user?.id || d.access === 'hr' ? (
              <Button
                size="xs"
                variant="subtle"
                color="red"
                leftSection={<IconTrash size={14} stroke={1.6} />}
                onClick={async () => {
                  try {
                    await tasksApi.remove(d.task.id);
                    toast.ok('Task removed.');
                    onChanged();
                    onClose();
                  } catch (e) {
                    toast.err(e);
                  }
                }}
              >
                Remove task
              </Button>
            ) : null}
          </Group>
          {d.task.completed_at ? (
            <Badge color="teal" variant="light">
              Done
            </Badge>
          ) : null}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          Loading…
        </Text>
      )}
    </Drawer>
  );
}
