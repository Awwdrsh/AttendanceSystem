import {
  ActionIcon,
  Badge,
  Button,
  Checkbox,
  Grid,
  Group,
  Loader,
  Menu,
  Modal,
  MultiSelect,
  Paper,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Textarea,
} from '@mantine/core';
import {
  IconDeviceFloppy,
  IconDots,
  IconDownload,
  IconFilter,
  IconPlayerPlay,
  IconPlus,
  IconShare,
  IconTrash,
  IconX,
} from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { catalog } from '../../api/catalog';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { ReportTable } from '../../components/ReportTable';
import { toast } from '../../lib/toast';

const OP_LABEL = {
  eq: 'is',
  neq: 'is not',
  contains: 'contains',
  starts: 'starts with',
  gt: 'is more than',
  gte: 'is at least',
  lt: 'is less than',
  lte: 'is at most',
  empty: 'is empty',
  not_empty: 'is not empty',
};
const OPS_FOR = {
  text: ['eq', 'neq', 'contains', 'starts', 'empty', 'not_empty'],
  number: ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
  date: ['eq', 'gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
  bool: ['eq', 'neq'],
};
const DATE_OP_LABEL = { eq: 'is on', gt: 'is after', gte: 'is on or after', lt: 'is before', lte: 'is on or before' };

const blank = (ds) => ({ dataset: ds.key, columns: ds.defaultColumns, filters: [], sort: null, includeExited: false });

/** Reports → Build a report: pick a dataset, columns, filters and a sort; preview 100 rows; export or save it. */
export default function ReportBuilderPage() {
  const [datasets, setDatasets] = useState(null);
  const [def, setDef] = useState(null);
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [saved, setSaved] = useState([]);
  const [current, setCurrent] = useState(null);
  const [saveOpen, setSaveOpen] = useState(false);

  const loadSaved = () =>
    catalog
      .saved()
      .then((r) => setSaved(r.reports))
      .catch(() => undefined);
  useEffect(() => {
    catalog
      .datasets()
      .then((r) => {
        setDatasets(r.datasets);
        if (r.datasets[0]) setDef(blank(r.datasets[0]));
      })
      .catch((e) => toast.err(e, 'Could not load the report builder'));
    void loadSaved();
  }, []);

  const ds = useMemo(() => datasets?.find((d) => d.key === def?.dataset) ?? null, [datasets, def?.dataset]);
  const colOptions = ds?.columns.map((c) => ({ value: c.key, label: c.label })) ?? [];
  const colType = (k) => ds?.columns.find((c) => c.key === k)?.type ?? 'text';

  const run = async (d = def) => {
    if (!d) return;
    setRunning(true);
    try {
      setResult(await catalog.run(d));
    } catch (e) {
      toast.err(e, 'Could not run the report');
    } finally {
      setRunning(false);
    }
  };

  const open = (s) => {
    setCurrent(s);
    setDef(s.definition);
    void run(s.definition);
  };
  const update = (patch) => setDef((d) => (d ? { ...d, ...patch } : d));
  const setFilter = (i, f) => update({ filters: (def?.filters ?? []).flatMap((x, j) => (j === i ? (f ? [f] : []) : [x])) });

  if (!datasets || !def || !ds)
    return (
      <Group justify="center" py="xl">
        <Loader size="sm" />
      </Group>
    );
  return (
    <Stack gap="md">
      <PageHeader
        title="Build a report"
        subtitle="Make your own list from employee records. Pick what to show, narrow it down, then download it or save it to run again."
        actions={
          <>
            <Button
              variant="default"
              leftSection={<IconDownload size={16} stroke={1.6} />}
              onClick={() =>
                catalog
                  .runCsv(def, `${(current?.name ?? ds.label).replace(/[^a-zA-Z0-9 _-]/g, '')}.csv`)
                  .catch((e) => toast.err(e, 'Export failed'))
              }
            >
              Export CSV
            </Button>
            <Button variant="default" leftSection={<IconDeviceFloppy size={16} stroke={1.6} />} onClick={() => setSaveOpen(true)}>
              {current?.mine ? 'Save changes' : 'Save'}
            </Button>
            <Button leftSection={<IconPlayerPlay size={16} stroke={1.6} />} onClick={() => void run()} loading={running}>
              Run
            </Button>
          </>
        }
      />
      <Grid gap="lg">
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Stack gap="md">
            <Paper withBorder p="md">
              <Stack gap="sm">
                <Select
                  label="Data"
                  data={datasets.map((d) => ({ value: d.key, label: d.label }))}
                  value={def.dataset}
                  allowDeselect={false}
                  onChange={(v) => {
                    const n = datasets.find((d) => d.key === v);
                    if (n) {
                      setDef(blank(n));
                      setCurrent(null);
                      setResult(null);
                    }
                  }}
                  description={ds.description}
                />
                <MultiSelect
                  label="Columns"
                  data={colOptions}
                  value={def.columns}
                  onChange={(v) => update({ columns: v })}
                  searchable
                  maxValues={30}
                  error={def.columns.length === 0 ? 'Pick at least one column' : undefined}
                />
                <Group gap="sm" align="flex-end" wrap="nowrap">
                  <Select
                    label="Sort by"
                    placeholder="Employee code"
                    data={colOptions}
                    value={def.sort?.column ?? null}
                    clearable
                    style={{ flex: 1 }}
                    onChange={(v) => update({ sort: v ? { column: v, dir: def.sort?.dir ?? 'asc' } : null })}
                  />
                  <Select
                    aria-label="Sort direction"
                    data={[
                      { value: 'asc', label: 'A to Z' },
                      { value: 'desc', label: 'Z to A' },
                    ]}
                    value={def.sort?.dir ?? 'asc'}
                    w={110}
                    disabled={!def.sort}
                    allowDeselect={false}
                    onChange={(v) => def.sort && update({ sort: { column: def.sort.column, dir: v ?? 'asc' } })}
                  />
                </Group>
                <Checkbox
                  label="Include people who have left"
                  checked={def.includeExited}
                  onChange={(e) => update({ includeExited: e.currentTarget.checked })}
                />
              </Stack>
            </Paper>
            <Paper withBorder p="md">
              <Stack gap="sm">
                <Group justify="space-between">
                  <Group gap={6}>
                    <IconFilter size={16} stroke={1.6} />
                    <Text fw={500} size="sm">
                      Filters
                    </Text>
                  </Group>
                  <Button
                    size="compact-sm"
                    variant="subtle"
                    leftSection={<IconPlus size={14} />}
                    disabled={def.filters.length >= 15}
                    onClick={() => update({ filters: [...def.filters, { column: ds.columns[0].key, op: 'eq', value: '' }] })}
                  >
                    Add
                  </Button>
                </Group>
                {def.filters.length === 0 ? (
                  <Text size="sm" c="dimmed">
                    No filters: every {def.includeExited ? '' : 'current '}employee is included.
                  </Text>
                ) : null}
                {def.filters.map((f, i) => {
                  const t = colType(f.column);
                  const ops = OPS_FOR[t] ?? OPS_FOR.text;
                  const needsValue = f.op !== 'empty' && f.op !== 'not_empty';
                  return (
                    <Paper key={i} withBorder p="xs" radius="md">
                      <Stack gap={6}>
                        <Group gap={6} wrap="nowrap">
                          <Select
                            size="xs"
                            data={colOptions}
                            value={f.column}
                            searchable
                            allowDeselect={false}
                            style={{ flex: 1 }}
                            aria-label="Column"
                            onChange={(v) => {
                              const nt = colType(v ?? f.column);
                              setFilter(i, { column: v ?? f.column, op: (OPS_FOR[nt] ?? [])[0] ?? 'eq', value: nt === 'bool' ? true : '' });
                            }}
                          />
                          <ActionIcon variant="subtle" color="gray" onClick={() => setFilter(i, null)} aria-label="Remove filter">
                            <IconX size={14} />
                          </ActionIcon>
                        </Group>
                        <Group gap={6} wrap="nowrap">
                          <Select
                            size="xs"
                            w={140}
                            data={ops.map((o) => ({ value: o, label: (t === 'date' ? DATE_OP_LABEL[o] : undefined) ?? OP_LABEL[o] }))}
                            value={f.op}
                            allowDeselect={false}
                            aria-label="Condition"
                            onChange={(v) => setFilter(i, { ...f, op: v ?? 'eq' })}
                          />
                          {needsValue ? (
                            t === 'date' ? (
                              <DateField
                                value={typeof f.value === 'string' && f.value ? f.value : null}
                                onChange={(v) => setFilter(i, { ...f, value: v ?? '' })}
                                w={160}
                              />
                            ) : t === 'bool' ? (
                              <Select
                                size="xs"
                                style={{ flex: 1 }}
                                data={[
                                  { value: 'true', label: 'Yes' },
                                  { value: 'false', label: 'No' },
                                ]}
                                value={f.value === false ? 'false' : 'true'}
                                allowDeselect={false}
                                aria-label="Value"
                                onChange={(v) => setFilter(i, { ...f, value: v === 'true' })}
                              />
                            ) : (
                              <TextInput
                                size="xs"
                                style={{ flex: 1 }}
                                value={String(f.value ?? '')}
                                aria-label="Value"
                                inputMode={t === 'number' ? 'decimal' : 'text'}
                                onChange={(e) => setFilter(i, { ...f, value: e.currentTarget.value })}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') void run();
                                }}
                              />
                            )
                          ) : null}
                        </Group>
                      </Stack>
                    </Paper>
                  );
                })}
              </Stack>
            </Paper>
            <Paper withBorder p="md">
              <Stack gap="xs">
                <Text fw={500} size="sm">
                  Saved reports
                </Text>
                {saved.length === 0 ? (
                  <Text size="sm" c="dimmed">
                    Reports you save, and reports colleagues share, appear here.
                  </Text>
                ) : null}
                {saved.map((s) => (
                  <Group key={s.id} justify="space-between" wrap="nowrap" gap="xs">
                    <Button
                      variant={current?.id === s.id ? 'light' : 'subtle'}
                      color="gray"
                      size="compact-sm"
                      onClick={() => open(s)}
                      style={{ flex: 1, justifyContent: 'flex-start' }}
                      styles={{ inner: { justifyContent: 'flex-start' } }}
                    >
                      <Text size="sm" truncate>
                        {s.name}
                      </Text>
                    </Button>
                    {s.shared ? (
                      <Badge size="xs" variant="light" leftSection={<IconShare size={10} />}>
                        {s.mine ? 'Shared' : (s.owner_name ?? 'Shared')}
                      </Badge>
                    ) : null}
                    {s.mine ? (
                      <Menu position="bottom-end" withinPortal>
                        <Menu.Target>
                          <ActionIcon variant="subtle" color="gray" size="sm" aria-label="More">
                            <IconDots size={14} />
                          </ActionIcon>
                        </Menu.Target>
                        <Menu.Dropdown>
                          <Menu.Item
                            color="red"
                            leftSection={<IconTrash size={14} />}
                            onClick={async () => {
                              try {
                                await catalog.remove(s.id);
                                if (current?.id === s.id) setCurrent(null);
                                await loadSaved();
                                toast.ok('Report deleted');
                              } catch (e) {
                                toast.err(e);
                              }
                            }}
                          >
                            Delete
                          </Menu.Item>
                        </Menu.Dropdown>
                      </Menu>
                    ) : null}
                  </Group>
                ))}
              </Stack>
            </Paper>
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 8 }}>
          {result ? (
            <Stack gap="xs">
              <Group gap="xs">
                <Badge variant="light" color="gray">
                  {result.total} {result.total === 1 ? 'row' : 'rows'}
                </Badge>
                {result.limited ? (
                  <Text size="xs" c="dimmed">
                    Showing the first {result.rows.length}. Export CSV for all of them.
                  </Text>
                ) : null}
              </Group>
              <ReportTable
                columns={result.columns}
                rows={result.rows}
                loading={running}
                emptyTitle="No employee records match these filters."
                maxHeight={640}
              />
            </Stack>
          ) : (
            <EmptyState
              title="Pick your columns and filters, then run"
              hint="The preview shows up to 100 rows. Export CSV downloads every row for Excel."
              action={{ label: 'Run', onClick: () => void run() }}
            />
          )}
        </Grid.Col>
      </Grid>
      <SaveModal
        opened={saveOpen}
        onClose={() => setSaveOpen(false)}
        current={current?.mine ? current : null}
        def={def}
        onSaved={(s) => {
          setCurrent(s);
          void loadSaved();
        }}
      />
    </Stack>
  );
}

function SaveModal({ opened, onClose, current, def, onSaved }) {
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [shared, setShared] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened) {
      setName(current?.name ?? '');
      setDesc(current?.description ?? '');
      setShared(current?.shared ?? false);
    }
  }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = async (asNew) => {
    setBusy(true);
    try {
      const body = { name: name.trim(), description: desc.trim() || null, shared, definition: def };
      const r = current && !asNew ? await catalog.update(current.id, body) : await catalog.save(body);
      onSaved(r.report);
      onClose();
      toast.ok(`"${r.report.name}" saved`);
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>{current ? 'Save report' : 'Save as a new report'}</Text>}>
      <Stack gap="sm">
        <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={120} data-autofocus required />
        <Textarea label="Description" value={desc} onChange={(e) => setDesc(e.currentTarget.value)} maxLength={500} autosize minRows={2} />
        <Switch label="Share with the other HR users" checked={shared} onChange={(e) => setShared(e.currentTarget.checked)} />
        <Group justify="flex-end" gap="xs">
          {current ? (
            <Button variant="default" onClick={() => void save(true)} loading={busy} disabled={!name.trim()}>
              Save as new
            </Button>
          ) : null}
          <Button onClick={() => void save(false)} loading={busy} disabled={!name.trim()}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
