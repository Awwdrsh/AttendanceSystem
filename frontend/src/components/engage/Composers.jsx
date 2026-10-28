import {
  ActionIcon,
  Button,
  Chip,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Textarea,
} from '@mantine/core';
import { IconEye, IconEyeOff } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { engage } from '../../api/engage';
import { insights } from '../../api/insights';
import { useAuth } from '../../auth/AuthProvider';
import { todayNpt, addDays } from '../../lib/format';
import { toast } from '../../lib/toast';
import { DateField } from '../BsDatePicker';

/** Give kudos to one or more colleagues, optionally tagged with a company value. */
export function KudosComposer({ opened, onClose, onDone, preset }) {
  const { user } = useAuth();
  const [people, setPeople] = useState([]);
  const [values, setValues] = useState([]);
  const [to, setTo] = useState(preset ?? []);
  const [valueId, setValueId] = useState(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!opened) return;
    setTo(preset ?? []);
    setValueId(null);
    setMessage('');
    engage
      .colleagues()
      .then((r) => setPeople(r.colleagues))
      .catch(() => setPeople([]));
    engage
      .values()
      .then((r) => setValues(r.values.filter((v) => v.active)))
      .catch(() => setValues([]));
  }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Modal opened={opened} onClose={onClose} title="Give kudos" centered size="lg">
      <Stack>
        <MultiSelect
          label="Who are you thanking?"
          placeholder="Search colleagues"
          searchable
          maxValues={10}
          required
          value={to}
          onChange={setTo}
          data={people
            .filter((p) => p.id !== user?.employeeId)
            .map((p) => ({ value: p.id, label: `${p.name}${p.designation ? ` · ${p.designation}` : ''}` }))}
          nothingFoundMessage="Nobody matches that name"
        />
        {values.length ? (
          <div>
            <Text size="sm" fw={500} mb={6}>
              Which value did they show?{' '}
              <Text span c="dimmed" size="sm">
                (optional)
              </Text>
            </Text>
            <Group gap={6}>
              {values.map((v) => (
                <Chip
                  key={v.id}
                  variant="outline"
                  size="sm"
                  checked={valueId === v.id}
                  onChange={() => setValueId(valueId === v.id ? null : v.id)}
                >
                  {v.emoji} {v.name}
                </Chip>
              ))}
            </Group>
          </div>
        ) : null}
        <Textarea
          label="Message"
          placeholder="Say what they did and why it mattered"
          autosize
          minRows={3}
          maxLength={1000}
          required
          value={message}
          onChange={(e) => setMessage(e.currentTarget.value)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!to.length || message.trim().length < 3}
            onClick={async () => {
              setBusy(true);
              try {
                await engage.giveKudos({ recipientIds: to, valueId, message: message.trim() });
                toast.ok('Kudos sent. They have been notified.');
                onDone();
                onClose();
              } catch (e) {
                toast.err(e, 'Kudos not sent');
              } finally {
                setBusy(false);
              }
            }}
          >
            Send kudos
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** Post an announcement. HR picks any audience; managers post to their own team. */
export function AnnouncementComposer({ opened, onClose, onDone }) {
  const { hasRole } = useAuth();
  const isHr = hasRole('hr');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [audienceKind, setAudienceKind] = useState(isHr ? 'all' : 'reports');
  const [audienceId, setAudienceId] = useState(null);
  const [pinned, setPinned] = useState(false);
  const [requiresAck, setRequiresAck] = useState(false);
  const [expiresOn, setExpiresOn] = useState(null);
  const [popup, setPopup] = useState(false);
  const [opts, setOpts] = useState({ branches: [], orgUnits: [] });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!opened) return;
    setTitle('');
    setBody('');
    setAudienceKind(isHr ? 'all' : 'reports');
    setAudienceId(null);
    setPinned(false);
    setRequiresAck(false);
    setExpiresOn(null);
    setPopup(false);
    if (isHr)
      insights
        .filters()
        .then(setOpts)
        .catch(() => undefined);
  }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps
  const needsTarget = audienceKind === 'branch' || audienceKind === 'department';
  return (
    <Modal opened={opened} onClose={onClose} title="New announcement" centered size="lg">
      <Stack>
        <TextInput label="Title" required maxLength={160} value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
        <Textarea
          label="Message"
          required
          autosize
          minRows={4}
          maxLength={8000}
          value={body}
          onChange={(e) => setBody(e.currentTarget.value)}
        />
        {isHr ? (
          <>
            <div>
              <Text size="sm" fw={500} mb={6}>
                Who is it for?
              </Text>
              <SegmentedControl
                fullWidth
                value={audienceKind}
                onChange={(v) => {
                  setAudienceKind(v);
                  setAudienceId(null);
                }}
                data={[
                  { value: 'all', label: 'Everyone' },
                  { value: 'branch', label: 'A branch' },
                  { value: 'department', label: 'A department' },
                ]}
              />
            </div>
            {needsTarget ? (
              <Select
                label={audienceKind === 'branch' ? 'Branch' : 'Department'}
                required
                searchable
                value={audienceId}
                onChange={setAudienceId}
                data={(audienceKind === 'branch' ? opts.branches : opts.orgUnits).map((o) => ({ value: o.id, label: o.name }))}
              />
            ) : null}
            <Switch label="Pin to the top of the feed and Home" checked={pinned} onChange={(e) => setPinned(e.currentTarget.checked)} />
            <Switch
              label="Show as a pop-up after sign-in"
              description="Once per person, or until they confirm if you ask for confirmation."
              checked={popup}
              onChange={(e) => setPopup(e.currentTarget.checked)}
            />
          </>
        ) : (
          <Text size="sm" c="dimmed">
            This goes to the people who report to you.
          </Text>
        )}
        <Switch
          label="Ask people to confirm they have read it"
          description="You can see who has and hasn't confirmed."
          checked={requiresAck}
          onChange={(e) => setRequiresAck(e.currentTarget.checked)}
        />
        <DateField
          label="Show until (optional)"
          value={expiresOn}
          onChange={setExpiresOn}
          minDate={todayNpt()}
          maxDate={addDays(todayNpt(), 365)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!title.trim() || !body.trim() || (needsTarget && !audienceId)}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await engage.postAnnouncement({
                  title: title.trim(),
                  body: body.trim(),
                  audienceKind,
                  audienceId,
                  pinned,
                  requiresAck,
                  expiresOn,
                  ...(isHr ? { popup } : {}),
                });
                toast.ok(`Posted. ${r.notified} ${r.notified === 1 ? 'person was' : 'people were'} notified.`);
                onDone();
                onClose();
              } catch (e) {
                toast.err(e, 'Not posted');
              } finally {
                setBusy(false);
              }
            }}
          >
            Post
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** HR: the company values used to tag kudos, and an optional monthly kudos limit per person. */
export function ValuesManager({ opened, onClose }) {
  const [values, setValues] = useState([]);
  const [limit, setLimit] = useState('');
  const [name, setName] = useState('');
  const [emoji, setEmoji] = useState('⭐');
  const [desc, setDesc] = useState('');
  const load = async () => {
    const r = await engage.values();
    setValues(r.values);
    setLimit(r.kudosMonthlyLimit ?? '');
  };
  useEffect(() => {
    if (opened) void load().catch((e) => toast.err(e));
  }, [opened]);
  return (
    <Modal opened={opened} onClose={onClose} title="Kudos settings" centered size="lg">
      <Stack>
        <Text size="sm" c="dimmed">
          Company values people can tag their kudos with. Hidden values stay on old kudos but can't be picked.
        </Text>
        <Stack gap={6}>
          {values.map((v) => (
            <Group key={v.id} justify="space-between" wrap="nowrap" style={{ opacity: v.active ? 1 : 0.55 }}>
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500}>
                  {v.emoji} {v.name}
                </Text>
                {v.description ? (
                  <Text size="xs" c="dimmed" truncate>
                    {v.description}
                  </Text>
                ) : null}
              </div>
              <ActionIcon
                variant="subtle"
                color="gray"
                aria-label={v.active ? `Hide ${v.name}` : `Show ${v.name}`}
                onClick={async () => {
                  try {
                    await engage.updateValue(v.id, { active: !v.active });
                    await load();
                  } catch (e) {
                    toast.err(e);
                  }
                }}
              >
                {v.active ? <IconEye size={16} /> : <IconEyeOff size={16} />}
              </ActionIcon>
            </Group>
          ))}
        </Stack>
        <Group align="flex-end" gap="xs" wrap="wrap">
          <TextInput label="Emoji" w={70} maxLength={8} value={emoji} onChange={(e) => setEmoji(e.currentTarget.value)} />
          <TextInput
            label="New value"
            style={{ flex: 1, minWidth: 140 }}
            maxLength={40}
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
          <TextInput
            label="Description"
            style={{ flex: 2, minWidth: 180 }}
            maxLength={200}
            value={desc}
            onChange={(e) => setDesc(e.currentTarget.value)}
          />
          <Button
            variant="default"
            disabled={!name.trim() || !emoji.trim()}
            onClick={async () => {
              try {
                await engage.createValue({ name: name.trim(), emoji: emoji.trim(), description: desc.trim() || null });
                setName('');
                setDesc('');
                await load();
              } catch (e) {
                toast.err(e);
              }
            }}
          >
            Add
          </Button>
        </Group>
        <Group align="flex-end" gap="xs">
          <NumberInput
            label="Kudos each person can give per month"
            description="Leave empty for no limit."
            min={1}
            max={100}
            w={280}
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : '')}
          />
          <Button
            variant="default"
            onClick={async () => {
              try {
                await engage.saveSettings({ kudosMonthlyLimit: limit === '' ? null : limit });
                toast.ok('Saved.');
              } catch (e) {
                toast.err(e);
              }
            }}
          >
            Save limit
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
