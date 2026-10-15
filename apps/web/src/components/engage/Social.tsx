import { ActionIcon, Button, Group, Loader, Stack, Text, Textarea, Tooltip, UnstyledButton } from '@mantine/core';
import { IconMessageCircle, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { engage, type Comment, type Reaction, type TargetType } from '../../api/engage';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';

export const REACTION_EMOJI: Record<Reaction, { emoji: string; label: string }> = {
  like: { emoji: '👍', label: 'Like' },
  love: { emoji: '❤️', label: 'Love' },
  clap: { emoji: '👏', label: 'Applause' },
  celebrate: { emoji: '🎉', label: 'Celebrate' },
  laugh: { emoji: '😄', label: 'Ha' },
};

function Comments({ targetType, targetId, onCount }: { targetType: TargetType; targetId: string; onCount: (n: number) => void }) {
  const [list, setList] = useState<Comment[] | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { const r = await engage.comments(targetType, targetId); setList(r.comments); onCount(r.comments.length); } catch (e) { toast.err(e, 'Could not load comments'); setList([]); }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void load(); }, [targetType, targetId]);
  if (list === null) return <Group py="xs"><Loader size="xs" /></Group>;
  return (
    <Stack gap={8} pt={4}>
      {list.map((c) => (
        <Group key={c.id} gap={8} align="flex-start" wrap="nowrap">
          <div style={{ flex: 1, minWidth: 0, background: 'var(--hj-canvas)', borderRadius: 10, padding: '6px 10px' }}>
            <Group gap={6} wrap="nowrap"><Text size="xs" fw={600} truncate>{c.name}</Text><Text size="xs" c="dimmed" style={{ flex: 'none' }}>{relativeTime(c.createdAt)}</Text></Group>
            <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{c.body}</Text>
          </div>
          {c.canDelete ? (
            <ActionIcon variant="subtle" color="gray" size="sm" aria-label="Remove comment" onClick={async () => {
              try { await engage.deleteComment(c.id); await load(); } catch (e) { toast.err(e); }
            }}><IconTrash size={14} stroke={1.6} /></ActionIcon>
          ) : null}
        </Group>
      ))}
      <Group gap={8} align="flex-end" wrap="nowrap">
        <Textarea style={{ flex: 1 }} size="sm" autosize minRows={1} maxRows={4} maxLength={1000} placeholder={targetType === 'celebration' ? 'Write a wish…' : 'Write a comment…'}
          value={text} onChange={(e) => setText(e.currentTarget.value)} aria-label="Comment" />
        <Button size="sm" loading={busy} disabled={!text.trim()} onClick={async () => {
          setBusy(true);
          try { await engage.comment(targetType, targetId, text.trim()); setText(''); await load(); } catch (e) { toast.err(e); } finally { setBusy(false); }
        }}>Send</Button>
      </Group>
    </Stack>
  );
}

/** Reaction chips, a comment toggle and the thread itself. Counts update in place. */
export function SocialBar({ targetType, targetId, reactions, mine, comments, compact }: {
  targetType: TargetType; targetId: string; reactions: Partial<Record<Reaction, number>>; mine: Reaction[]; comments: number; compact?: boolean;
}) {
  const [state, setState] = useState({ reactions, mine });
  const [count, setCount] = useState(comments);
  const [open, setOpen] = useState(false);
  const toggle = async (r: Reaction) => {
    try { const x = await engage.react(targetType, targetId, r); setState({ reactions: x.reactions, mine: x.mine }); } catch (e) { toast.err(e); }
  };
  const shown = (Object.keys(REACTION_EMOJI) as Reaction[]).filter((r) => !compact || (state.reactions[r] ?? 0) > 0 || r === 'like' || r === 'celebrate');
  return (
    <Stack gap={6}>
      <Group gap={6} wrap="wrap">
        {shown.map((r) => {
          const n = state.reactions[r] ?? 0; const on = state.mine.includes(r);
          return (
            <Tooltip key={r} label={REACTION_EMOJI[r].label} openDelay={400}>
              <UnstyledButton onClick={() => void toggle(r)} aria-pressed={on} aria-label={`${REACTION_EMOJI[r].label}${n ? `, ${n}` : ''}`}
                style={{ border: `1px solid ${on ? 'var(--hj-hydro)' : 'var(--hj-line)'}`, background: on ? 'var(--hj-hydro-soft)' : 'transparent', borderRadius: 999, padding: '2px 9px', fontSize: 13, lineHeight: '20px' }}>
                {REACTION_EMOJI[r].emoji}{n ? <span className="num" style={{ marginLeft: 4, fontSize: 12 }}>{n}</span> : null}
              </UnstyledButton>
            </Tooltip>
          );
        })}
        <Button variant="subtle" color="gray" size="compact-sm" leftSection={<IconMessageCircle size={15} stroke={1.6} />} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {count ? `${count} ${count === 1 ? 'comment' : 'comments'}` : targetType === 'celebration' ? 'Wish' : 'Comment'}
        </Button>
      </Group>
      {open ? <Comments targetType={targetType} targetId={targetId} onCount={setCount} /> : null}
    </Stack>
  );
}
