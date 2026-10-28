import { Box, Button, Flex, Group, NumberInput, Paper, Select, Switch, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import { time as timeApi } from '../../api/time';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

function Row({ label, description, children }) {
  return (
    <Flex
      direction={{ base: 'column', sm: 'row' }}
      gap={{ base: 8, sm: 'xl' }}
      justify="space-between"
      align={{ base: 'stretch', sm: 'center' }}
      px="lg"
      py="md"
      style={{ borderTop: '1px solid var(--hj-line-soft)' }}
    >
      <Box style={{ flex: 1, minWidth: 0 }}>
        <Text size="sm" fw={500}>
          {label}
        </Text>
        {description ? (
          <Text size="xs" c="dimmed" mt={2}>
            {description}
          </Text>
        ) : null}
      </Box>
      <Box style={{ flex: 'none' }}>{children}</Box>
    </Flex>
  );
}

/** Company settings for project time tracking and weekly timesheets. */
export function TimeTrackingSettings() {
  const q = useQuery(() => timeApi.settings(), []);
  const [p, setP] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (q.data) setP(q.data.policy);
  }, [q.data]);
  if (!p) return null;
  const set = (k, v) => setP({ ...p, [k]: v });
  const save = async () => {
    setBusy(true);
    try {
      await timeApi.saveSettings(p);
      toast.ok('Time tracking settings saved');
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Paper withBorder>
      <Box px="lg" py="md">
        <Text fw={600}>Time tracking</Text>
        <Text size="sm" c="dimmed">
          Project timers, timesheets and how hours are rounded for approval and reports. Raw minutes are always kept.
        </Text>
      </Box>
      <Row label="Time tracking" description="Employees can run a timer and log time against projects.">
        <Switch checked={p.enabled} onChange={(e) => set('enabled', e.currentTarget.checked)} />
      </Row>
      <Row label="Require a project" description="Every entry must name a project.">
        <Switch checked={p.requireProject} onChange={(e) => set('requireProject', e.currentTarget.checked)} />
      </Row>
      <Row label="Rounding" description="Each entry is rounded in timesheets and reports.">
        <Group gap="xs" wrap="nowrap">
          <Select
            w={120}
            value={String(p.roundingStep)}
            onChange={(v) => set('roundingStep', Number(v ?? 0))}
            data={[
              { value: '0', label: 'None' },
              { value: '5', label: '5 min' },
              { value: '6', label: '6 min' },
              { value: '10', label: '10 min' },
              { value: '15', label: '15 min' },
            ]}
            allowDeselect={false}
          />
          <Select
            w={120}
            disabled={p.roundingStep === 0}
            value={p.roundingMode}
            onChange={(v) => set('roundingMode', v ?? 'nearest')}
            data={[
              { value: 'nearest', label: 'Nearest' },
              { value: 'up', label: 'Up' },
              { value: 'down', label: 'Down' },
            ]}
            allowDeselect={false}
          />
        </Group>
      </Row>
      <Row label="Friday reminder" description="Remind people who have not submitted their week.">
        <Switch checked={p.fridayReminder} onChange={(e) => set('fridayReminder', e.currentTarget.checked)} />
      </Row>
      <Row label="Stop forgotten timers after" description="A timer running longer is stopped at this length and the person is told.">
        <NumberInput
          w={120}
          min={1}
          max={24}
          suffix=" h"
          value={p.autoStopHours}
          onChange={(v) => set('autoStopHours', Math.min(24, Math.max(1, Number(v) || 12)))}
        />
      </Row>
      <Group justify="flex-end" px="lg" py="md" style={{ borderTop: '1px solid var(--hj-line-soft)' }}>
        <Button loading={busy} onClick={save}>
          Save
        </Button>
      </Group>
    </Paper>
  );
}
