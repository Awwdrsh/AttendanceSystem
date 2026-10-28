import { Avatar, Badge, Button, Group, Modal, Paper, Select, Stack, Switch, Text, Textarea } from '@mantine/core';
import { IconBalloon, IconBulb, IconConfetti, IconTrophy, IconUserPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { engage } from '../../api/engage';
import { useAuth } from '../../auth/AuthProvider';
import { bsMonthLabel, fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { initials } from './FeedCards';
import { SocialBar } from './Social';

const ICON = { size: 16, stroke: 1.6 };

function when(c) {
  if (c.kind === 'joined') return c.daysAway === 0 ? 'Joined today' : `Joined ${fmtDate(c.date)}`;
  return c.daysAway === 0 ? 'Today' : c.daysAway === 1 ? 'Tomorrow' : c.bsLabel;
}
function what(c) {
  if (c.kind === 'birthday') return 'Birthday';
  if (c.kind === 'anniversary') return `${c.years} ${c.years === 1 ? 'year' : 'years'} at the company`;
  return 'New joiner';
}

/** Birthdays (opt-in), work anniversaries and new joiners for the next two weeks, with wishes; plus my own birthday setting. */
export function CelebrationsPanel() {
  const { user } = useAuth();
  const q = useQuery(() => engage.celebrations(14), []);
  const prefs = useQuery(() => engage.prefs(), [], { enabled: !!user?.employeeId });
  const [open, setOpen] = useState(null);
  const list = q.data?.celebrations ?? [];
  return (
    <Paper withBorder p="md" radius="lg">
      <Group gap={8} mb="sm">
        <IconConfetti {...ICON} />
        <Text fw={600} size="sm">
          Celebrations
        </Text>
      </Group>
      {list.length === 0 ? (
        <Text size="sm" c="dimmed">
          No birthdays, anniversaries or new joiners in the next two weeks.
        </Text>
      ) : (
        <Stack gap="sm">
          {list.map((c) => (
            <div key={c.targetId}>
              <Group gap="sm" wrap="nowrap" align="flex-start">
                <Avatar radius="xl" size={34} color={c.kind === 'birthday' ? 'pink' : c.kind === 'joined' ? 'blue' : 'brand'}>
                  {c.kind === 'birthday' ? <IconBalloon size={16} /> : c.kind === 'joined' ? <IconUserPlus size={16} /> : initials(c.name)}
                </Avatar>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Text size="sm" fw={600} truncate>
                    {c.name}
                    {c.isMe ? ' (you)' : ''}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {what(c)} · {when(c)}
                  </Text>
                  {c.daysAway <= 0 ? (
                    open === c.targetId ? (
                      <SocialBar
                        targetType="celebration"
                        targetId={c.targetId}
                        reactions={c.reactions}
                        mine={c.mine}
                        comments={c.comments}
                        compact
                      />
                    ) : (
                      <Button variant="subtle" size="compact-xs" px={0} onClick={() => setOpen(c.targetId)}>
                        {c.comments ? `${c.comments} wishes · ` : ''}
                        {c.kind === 'joined' ? 'Say welcome' : 'Send a wish'}
                      </Button>
                    )
                  ) : null}
                </div>
              </Group>
            </div>
          ))}
        </Stack>
      )}
      {user?.employeeId && prefs.data ? (
        <Switch
          mt="md"
          size="sm"
          checked={prefs.data.showBirthday}
          disabled={!prefs.data.hasBirthday}
          label="Show my birthday to colleagues"
          description={prefs.data.hasBirthday ? 'Only the day and month, never your age.' : 'Ask HR to add your date of birth first.'}
          onChange={async (e) => {
            const v = e.currentTarget.checked;
            try {
              await engage.savePrefs(v);
              prefs.setData({ ...prefs.data, showBirthday: v });
              await q.reload();
            } catch (err) {
              toast.err(err);
            }
          }}
        />
      ) : null}
    </Paper>
  );
}

/** Most appreciated people this BS month. */
export function LeaderboardPanel() {
  const q = useQuery(() => engage.leaderboard(), []);
  const d = q.data;
  return (
    <Paper withBorder p="md" radius="lg">
      <Group gap={8} mb="sm" justify="space-between">
        <Group gap={8}>
          <IconTrophy {...ICON} />
          <Text fw={600} size="sm">
            Most appreciated
          </Text>
        </Group>
        {d ? (
          <Text size="xs" c="dimmed">
            {bsMonthLabel(d.bsYear, d.bsMonth)}
          </Text>
        ) : null}
      </Group>
      {!d || d.received.length === 0 ? (
        <Text size="sm" c="dimmed">
          No kudos yet this month. Be the first to thank someone.
        </Text>
      ) : (
        <Stack gap={8}>
          {d.received.slice(0, 5).map((r, i) => (
            <Group key={r.employeeId} gap="sm" wrap="nowrap">
              <Text size="sm" c="dimmed" w={16} className="num">
                {i + 1}
              </Text>
              <Text size="sm" style={{ flex: 1 }} truncate>
                {r.name}
              </Text>
              <Badge variant="light" size="sm">
                {r.count}
              </Badge>
            </Group>
          ))}
          {d.values.some((v) => v.count > 0) ? (
            <Group gap={6} mt={4}>
              {d.values
                .filter((v) => v.count > 0)
                .slice(0, 4)
                .map((v) => (
                  <Badge key={v.id} variant="outline" color="gray" size="sm">
                    {v.emoji} {v.name} · {v.count}
                  </Badge>
                ))}
            </Group>
          ) : null}
        </Stack>
      )}
    </Paper>
  );
}

const CATEGORIES = [
  { value: 'general', label: 'General' },
  { value: 'workplace', label: 'Workplace' },
  { value: 'process', label: 'How we work' },
  { value: 'benefits', label: 'Pay and benefits' },
  { value: 'culture', label: 'Culture' },
  { value: 'other', label: 'Other' },
];
export const categoryLabel = (v) => CATEGORIES.find((c) => c.value === v)?.label ?? v;

/** Anonymous ideas to HR: nothing about the sender is stored. Answers HR chose to share are listed. */
export function SuggestionBoxPanel() {
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState('general');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const answered = useQuery(() => engage.publicSuggestions(), []);
  return (
    <Paper withBorder p="md" radius="lg">
      <Group gap={8} mb={4}>
        <IconBulb {...ICON} />
        <Text fw={600} size="sm">
          Suggestion box
        </Text>
      </Group>
      <Text size="sm" c="dimmed" mb="sm">
        Share an idea or concern with HR anonymously. Your name is never stored with it.
      </Text>
      <Button variant="default" size="sm" fullWidth onClick={() => setOpen(true)}>
        Write a suggestion
      </Button>
      {answered.data?.suggestions.length ? (
        <Stack gap="sm" mt="md">
          <Text size="xs" fw={600} c="dimmed" tt="uppercase">
            HR answered
          </Text>
          {answered.data.suggestions.slice(0, 3).map((s) => (
            <div key={s.id}>
              <Text size="sm" lineClamp={2}>
                “{s.body}”
              </Text>
              <Text size="sm" c="dimmed" mt={2} lineClamp={3}>
                {s.reply}
              </Text>
            </div>
          ))}
        </Stack>
      ) : null}
      <Modal opened={open} onClose={() => setOpen(false)} title="Anonymous suggestion" centered>
        <Stack>
          <Text size="sm" c="dimmed">
            HR sees only the topic, your words and the day it arrived. Don't include anything that identifies you unless you want to.
          </Text>
          <Select label="Topic" data={CATEGORIES} value={category} onChange={setCategory} allowDeselect={false} />
          <Textarea
            label="Your suggestion"
            autosize
            minRows={4}
            maxLength={3000}
            value={body}
            onChange={(e) => setBody(e.currentTarget.value)}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={busy}
              disabled={body.trim().length < 5}
              onClick={async () => {
                setBusy(true);
                try {
                  await engage.suggest({ category: category ?? 'general', body: body.trim() });
                  toast.ok('Sent anonymously. Thank you.');
                  setBody('');
                  setOpen(false);
                } catch (e) {
                  toast.err(e, 'Not sent');
                } finally {
                  setBusy(false);
                }
              }}
            >
              Send anonymously
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Paper>
  );
}
