import { Badge, Box, Button, Group, Loader, Paper, Stack, Switch, Text } from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';
import { features as featuresApi, type FeatureInfo, type FeatureKey } from '../../api/catalog';
import { PageHeader } from '../../components/PageHeader';
import { useFeatures } from '../../lib/features';
import { toast } from '../../lib/toast';

/** Settings → Features: switch whole modules on or off for the company. Data is kept; switching back on brings it back. */
export default function FeaturesPage() {
  const { reload } = useFeatures();
  const [list, setList] = useState<FeatureInfo[] | null>(null);
  const [draft, setDraft] = useState<Partial<Record<FeatureKey, boolean>>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    featuresApi.get().then((r) => { setList(r.features); setDraft(r.enabled); }).catch((e) => toast.err(e, 'Could not load the features'));
  }, []);

  const groups = useMemo(() => {
    const m = new Map<string, FeatureInfo[]>();
    for (const f of list ?? []) m.set(f.group, [...(m.get(f.group) ?? []), f]);
    return [...m.entries()];
  }, [list]);
  const changed = (list ?? []).filter((f) => draft[f.key] !== f.enabled);

  const save = async () => {
    setSaving(true);
    try {
      const r = await featuresApi.save(Object.fromEntries(changed.map((f) => [f.key, draft[f.key] ?? true])));
      setList(r.features); setDraft(r.enabled);
      await reload();
      toast.ok('Everyone sees the change the next time they open a page.', 'Features saved');
    } catch (e) { toast.err(e); } finally { setSaving(false); }
  };

  if (!list) return <Group justify="center" py="xl"><Loader size="sm" /></Group>;
  return (
    <Stack gap="md">
      <PageHeader title="Features" subtitle="Switch off the parts of Hajiri your company does not use. They disappear from everyone's menu; nothing is deleted, and switching one back on brings its data back."
        actions={<>
          {changed.length ? <Button variant="subtle" color="gray" onClick={() => setDraft(Object.fromEntries(list.map((f) => [f.key, f.enabled])))}>Undo changes</Button> : null}
          <Button onClick={() => void save()} loading={saving} disabled={!changed.length}>Save{changed.length ? ` (${changed.length})` : ''}</Button>
        </>} />
      {groups.map(([group, items]) => (
        <Paper withBorder key={group}>
          <Box px="lg" py="sm" style={{ borderBottom: '1px solid var(--hj-line-soft)' }}><Text fw={600} size="sm">{group}</Text></Box>
          {items.map((f, i) => (
            <Group key={f.key} justify="space-between" wrap="nowrap" gap="md" px="lg" py="md" style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
              <div style={{ minWidth: 0 }}>
                <Group gap={8}>
                  <Text size="sm" fw={500}>{f.label}</Text>
                  {draft[f.key] !== f.enabled ? <Badge size="xs" variant="light" color="yellow">Not saved</Badge> : null}
                </Group>
                <Text size="sm" c="dimmed">{f.description}</Text>
              </div>
              <Switch checked={draft[f.key] !== false} onChange={(e) => { const v = e.currentTarget.checked; setDraft((d) => ({ ...d, [f.key]: v })); }}
                aria-label={f.label} size="md" />
            </Group>
          ))}
        </Paper>
      ))}
      <Text size="xs" c="dimmed">Attendance, leave, payroll, people, approvals and reports are the core of Hajiri and are always on.</Text>
    </Stack>
  );
}
