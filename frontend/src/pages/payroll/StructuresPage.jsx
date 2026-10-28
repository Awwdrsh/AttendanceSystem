import {
  Badge,
  Button,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core';
import { IconLock, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { payroll as payApi } from '../../api';
import { DataTable } from '../../components/DataTable';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const TYPES = [
  { value: 'earning', label: 'Earning' },
  { value: 'deduction', label: 'Deduction' },
  { value: 'employer_contribution', label: 'Employer contribution' },
  { value: 'statistical', label: 'Statistical (not paid)' },
];
const typeLabel = (t) => TYPES.find((x) => x.value === t)?.label ?? t;

export default function StructuresPage() {
  const comps = useQuery(() => payApi.components(), []);
  const structs = useQuery(() => payApi.structures(), []);
  const [edit, setEdit] = useState(null);
  const [busy, setBusy] = useState(false);
  const [newS, setNewS] = useState(null);
  const set = (k, v) => setEdit((e) => e && { ...e, [k]: v });
  const save = async () => {
    if (!edit) return;
    setBusy(true);
    try {
      const { isNew, statutory: _s, ...b } = edit;
      if (isNew) await payApi.createComponent(b);
      else {
        const { code, ...rest } = b;
        await payApi.updateComponent(code, rest);
      }
      toast.ok('Component saved.');
      setEdit(null);
      await comps.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const saveStructure = async () => {
    if (!newS) return;
    setBusy(true);
    try {
      const existing = structs.data?.structures.find((s) => s.code === newS.code);
      if (existing)
        await payApi.addVersion(
          existing.id,
          newS.components.map((code) => ({ code })),
        );
      else await payApi.createStructure({ code: newS.code, nameEn: newS.nameEn, components: newS.components.map((code) => ({ code })) });
      toast.ok('Structure saved.');
      setNewS(null);
      await structs.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const isExistingStructure = !!newS && !!structs.data?.structures.some((s) => s.code === newS.code);

  return (
    <Stack gap="lg">
      <PageHeader
        title="Salary structures"
        subtitle="Components are the building blocks of a payslip, each with an optional formula. Structures bundle components; a structure version freezes once it is assigned to someone."
      />
      <Tabs defaultValue="components">
        <Tabs.List>
          <Tabs.Tab value="components">Components</Tabs.Tab>
          <Tabs.Tab value="structures">Structures</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="components" pt="lg">
          <SectionTitle
            hint="Statutory components (SSF, TDS and similar) are locked."
            actions={
              <Button
                leftSection={<IconPlus size={16} stroke={1.6} />}
                onClick={() =>
                  setEdit({
                    isNew: true,
                    code: '',
                    nameEn: '',
                    nameNe: '',
                    type: 'earning',
                    formula: '',
                    condition: '',
                    dependsOnPaymentDays: false,
                    taxable: true,
                    rounding: 'paisa',
                    sortOrder: 100,
                    active: true,
                  })
                }
              >
                New component
              </Button>
            }
          >
            Components
          </SectionTitle>
          <DataTable
            rows={comps.data?.components ?? []}
            rowKey={(c) => c.code}
            loading={comps.loading}
            error={comps.error}
            emptyTitle="No components yet"
            emptyHint="Load the defaults from Setup, or add your first component."
            onRowClick={(c) =>
              setEdit({
                code: c.code,
                statutory: c.statutory,
                nameEn: c.name_en,
                nameNe: c.name_ne ?? '',
                type: c.type,
                formula: c.formula ?? '',
                condition: c.condition ?? '',
                dependsOnPaymentDays: c.depends_on_payment_days,
                taxable: c.taxable,
                rounding: c.rounding,
                sortOrder: c.sort_order,
                active: c.active,
              })
            }
            columns={[
              {
                key: 'code',
                header: 'Component',
                render: (c) => (
                  <div>
                    <Group gap={6} wrap="nowrap">
                      <Text size="sm" fw={500} ff="monospace">
                        {c.code}
                      </Text>
                      {c.statutory ? <IconLock size={14} stroke={1.6} color="var(--hj-muted)" aria-label="Statutory" /> : null}
                      {!c.active ? (
                        <Badge size="sm" variant="light" color="gray">
                          Inactive
                        </Badge>
                      ) : null}
                    </Group>
                    <Text size="xs" c="dimmed">
                      {c.name_en}
                    </Text>
                  </div>
                ),
              },
              { key: 'type', header: 'Type', render: (c) => typeLabel(c.type) },
              {
                key: 'formula',
                header: 'Formula',
                render: (c) =>
                  c.formula ? (
                    <Text size="xs" ff="monospace">
                      {c.formula}
                    </Text>
                  ) : (
                    <Text size="xs" c="dimmed">
                      Amount from assignment or input
                    </Text>
                  ),
              },
              {
                key: 'flags',
                header: '',
                render: (c) => (
                  <Group gap={4} wrap="nowrap">
                    {c.taxable ? (
                      <Badge size="sm" variant="light" color="gray">
                        Taxable
                      </Badge>
                    ) : null}
                    {c.depends_on_payment_days ? (
                      <Badge size="sm" variant="light" color="gray">
                        Prorated
                      </Badge>
                    ) : null}
                  </Group>
                ),
              },
            ]}
          />
        </Tabs.Panel>
        <Tabs.Panel value="structures" pt="lg">
          <SectionTitle
            hint="Editing a structure adds a new version; existing assignments keep theirs."
            actions={
              <Button
                leftSection={<IconPlus size={16} stroke={1.6} />}
                onClick={() => setNewS({ code: '', nameEn: '', components: ['BASIC'] })}
              >
                New structure
              </Button>
            }
          >
            Structures
          </SectionTitle>
          <DataTable
            rows={structs.data?.structures ?? []}
            rowKey={(s) => s.id}
            loading={structs.loading}
            error={structs.error}
            emptyTitle="No structures yet"
            emptyHint="Create a structure from your components, then assign it to employees."
            columns={[
              {
                key: 'code',
                header: 'Structure',
                render: (s) => (
                  <>
                    <Text size="sm" fw={500}>
                      {s.name_en}
                    </Text>
                    <Text size="xs" c="dimmed" ff="monospace">
                      {s.code}
                    </Text>
                  </>
                ),
              },
              {
                key: 'versions',
                header: 'Versions',
                render: (s) => (
                  <Stack gap={4}>
                    {(s.versions ?? []).map((v) => (
                      <Group key={v.id} gap={6} wrap="nowrap" align="flex-start">
                        <Text size="xs" fw={500} w={24}>
                          v{v.version}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {v.components.map((c) => c.code).join(', ')}
                        </Text>
                        {v.frozenAt ? (
                          <IconLock
                            size={13}
                            stroke={1.6}
                            color="var(--hj-muted)"
                            aria-label="Frozen"
                            style={{ flex: 'none', marginTop: 2 }}
                          />
                        ) : null}
                      </Group>
                    ))}
                  </Stack>
                ),
              },
              {
                key: 'a',
                header: '',
                align: 'right',
                render: (s) => (
                  <Button
                    size="xs"
                    variant="default"
                    onClick={() =>
                      setNewS({
                        code: s.code,
                        nameEn: s.name_en,
                        components: (s.versions?.[s.versions.length - 1]?.components ?? []).map((c) => c.code),
                      })
                    }
                  >
                    New version
                  </Button>
                ),
              },
            ]}
          />
        </Tabs.Panel>
      </Tabs>

      <Modal
        opened={!!edit}
        onClose={() => setEdit(null)}
        title={edit?.isNew ? 'New component' : `Edit ${String(edit?.code)}`}
        size="lg"
        centered
      >
        {edit ? (
          <Stack>
            {edit.statutory ? (
              <Group gap={6} wrap="nowrap">
                <IconLock size={16} stroke={1.6} color="var(--hj-muted)" />
                <Text size="sm" c="dimmed">
                  Statutory component: its type and formula are set by law and cannot be changed.
                </Text>
              </Group>
            ) : null}
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <TextInput
                label="Code"
                description="Capitals, digits and underscores; used in formulas."
                value={String(edit.code)}
                onChange={(e) => set('code', e.currentTarget.value.toUpperCase().replace(/[^A-Z0-9_]/g, ''))}
                disabled={!edit.isNew}
                required
              />
              <TextInput
                label="Name"
                description="Shown on payslips."
                value={String(edit.nameEn)}
                onChange={(e) => set('nameEn', e.currentTarget.value)}
                required
              />
              <Select
                label="Type"
                value={String(edit.type)}
                onChange={(v) => v && set('type', v)}
                allowDeselect={false}
                disabled={!!edit.statutory}
                data={TYPES}
              />
              <Select
                label="Rounding"
                value={String(edit.rounding)}
                onChange={(v) => v && set('rounding', v)}
                allowDeselect={false}
                data={[
                  { value: 'paisa', label: 'To the paisa' },
                  { value: 'rupee', label: 'To the rupee' },
                ]}
              />
            </SimpleGrid>
            <TextInput
              label="Formula"
              description={'For example BASIC * 0.11. Leave blank to take the amount from the assignment or a one-time input.'}
              value={String(edit.formula ?? '')}
              onChange={(e) => set('formula', e.currentTarget.value)}
              disabled={!!edit.statutory}
              ff="monospace"
            />
            <TextInput
              label="Condition"
              description={'Optional. Applies the component only when true, for example regime == "ssf".'}
              value={String(edit.condition ?? '')}
              onChange={(e) => set('condition', e.currentTarget.value)}
              ff="monospace"
            />
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <NumberInput
                label="Sort order"
                description="Lower numbers appear first on the payslip."
                value={Number(edit.sortOrder) || 0}
                onChange={(v) => set('sortOrder', Number(v) || 0)}
              />
            </SimpleGrid>
            <Group gap="lg" wrap="wrap">
              <Switch
                label="Prorate by paid days"
                checked={!!edit.dependsOnPaymentDays}
                onChange={(e) => set('dependsOnPaymentDays', e.currentTarget.checked)}
              />
              <Switch label="Taxable" checked={!!edit.taxable} onChange={(e) => set('taxable', e.currentTarget.checked)} />
              <Switch label="Active" checked={!!edit.active} onChange={(e) => set('active', e.currentTarget.checked)} />
            </Group>
            <Group justify="flex-end" mt="xs">
              <Button variant="default" onClick={() => setEdit(null)}>
                Cancel
              </Button>
              <Button loading={busy} onClick={save}>
                Save component
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>

      <Modal
        opened={!!newS}
        onClose={() => setNewS(null)}
        title={isExistingStructure ? 'New structure version' : 'New salary structure'}
        centered
      >
        {newS ? (
          <Stack>
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <TextInput
                label="Code"
                value={newS.code}
                onChange={(e) => setNewS({ ...newS, code: e.currentTarget.value.toUpperCase() })}
                required
              />
              <TextInput label="Name" value={newS.nameEn} onChange={(e) => setNewS({ ...newS, nameEn: e.currentTarget.value })} required />
            </SimpleGrid>
            <MultiSelect
              label="Components"
              description="In payslip order."
              value={newS.components}
              onChange={(v) => setNewS({ ...newS, components: v })}
              data={(comps.data?.components ?? []).map((c) => c.code)}
              searchable
            />
            {isExistingStructure ? (
              <Text size="xs" c="dimmed">
                A structure with this code exists, so this saves as its next version.
              </Text>
            ) : null}
            <Group justify="flex-end" mt="xs">
              <Button variant="default" onClick={() => setNewS(null)}>
                Cancel
              </Button>
              <Button loading={busy} disabled={!newS.code || !newS.nameEn || !newS.components.length} onClick={saveStructure}>
                Save structure
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
