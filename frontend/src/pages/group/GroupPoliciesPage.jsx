import { Accordion, Badge, Button, Checkbox, Group, MultiSelect, Paper, Radio, Select, SimpleGrid, Stack, Table, Text } from '@mantine/core';
import { IconCopy, IconEye } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { group as groupApi } from '../../api/group';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { POLICY_ACTION, policyTotalsLabel, previewChanges } from '../../lib/group';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const TONE_COLOR = { ok: 'teal', warn: 'orange', info: 'blue', idle: 'gray', bad: 'red' };

function ActionBadge({ action }) {
  const a = POLICY_ACTION[action] ?? { label: action, tone: 'idle' };
  return (
    <Badge size="sm" variant="light" color={TONE_COLOR[a.tone]}>
      {a.label}
    </Badge>
  );
}

function Counts({ counts }) {
  return (
    <Group gap={4}>
      {Object.entries(counts)
        .filter(([, n]) => n > 0)
        .map(([k, n]) => (
          <Badge key={k} size="xs" variant="outline" color={TONE_COLOR[POLICY_ACTION[k]?.tone ?? 'idle']}>
            {n} {POLICY_ACTION[k]?.label.toLowerCase() ?? k}
          </Badge>
        ))}
    </Group>
  );
}

function PlanView({ plan, applied }) {
  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        {applied ? 'Copied' : 'Copying'} from {plan.source.name}
        {plan.onConflict === 'overwrite' ? ', overwriting what differs' : ', keeping what already exists'}:{' '}
        {policyTotalsLabel(plan.totals)}.
      </Text>
      {plan.targets.map((t) => (
        <Paper withBorder p="md" key={t.companyId}>
          <Group justify="space-between" mb="xs">
            <Text fw={600}>{t.name}</Text>
            <Text size="xs" c="dimmed">
              {policyTotalsLabel(t.totals)}
            </Text>
          </Group>
          <Accordion variant="separated" multiple chevronPosition="left">
            {t.sections.map((s) => (
              <Accordion.Item key={s.key} value={s.key}>
                <Accordion.Control>
                  <Group justify="space-between" wrap="nowrap" pr="sm">
                    <Text size="sm" fw={500}>
                      {s.label}
                    </Text>
                    <Counts counts={s.counts} />
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  {s.items.length ? (
                    <Table fz="sm" verticalSpacing={4}>
                      <Table.Tbody>
                        {s.items.map((i) => (
                          <Table.Tr key={i.key}>
                            <Table.Td>{i.label}</Table.Td>
                            <Table.Td w={150}>
                              <ActionBadge action={i.action} />
                            </Table.Td>
                            <Table.Td c="dimmed">{i.reason ?? ''}</Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  ) : (
                    <Text size="sm" c="dimmed">
                      The source company has nothing in this section.
                    </Text>
                  )}
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
        </Paper>
      ))}
    </Stack>
  );
}

export default function GroupPoliciesPage() {
  const companiesQ = useQuery(() => groupApi.companies(), []);
  const sectionsQ = useQuery(() => groupApi.policySections(), []);
  const [sourceCompanyId, setSource] = useState(null);
  const [targetCompanyIds, setTargets] = useState([]);
  const [sections, setSections] = useState([]);
  const [onConflict, setOnConflict] = useState('skip');
  const [plan, setPlan] = useState(null);
  const [applied, setApplied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const companies = companiesQ.data?.items ?? [];
  const body = { sourceCompanyId, targetCompanyIds, sections, onConflict };
  const key = JSON.stringify(body);
  // Any change to the choices makes the preview stale.
  useEffect(() => {
    setPlan(null);
    setApplied(false);
  }, [key]);
  useEffect(() => {
    if (!sourceCompanyId && companies.length) setSource(companies.find((c) => c.active)?.id ?? companies[0].id);
  }, [companies, sourceCompanyId]);

  const ready = sourceCompanyId && targetCompanyIds.length && sections.length;
  const preview = async () => {
    setBusy(true);
    try {
      setPlan(await groupApi.previewPolicies(body));
      setApplied(false);
    } catch (e) {
      toast.err(e, 'Could not preview');
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    try {
      const r = await groupApi.applyPolicies(body);
      setPlan(r);
      setApplied(true);
      toast.ok(policyTotalsLabel(r.totals).replace('to create', 'created').replace('to overwrite', 'overwritten'), 'Policies copied');
    } catch (e) {
      toast.err(e, 'Not copied');
    }
  };

  if (companiesQ.error) return <EmptyState title="Could not load the companies" hint={companiesQ.error} />;
  const allSections = sectionsQ.data?.items ?? [];

  return (
    <Stack gap="lg">
      <PageHeader
        title="Policies"
        subtitle="Copy policy templates (leave types, salary components and structures, approval flows, holidays, shifts and settings) from one company of the group to others. Preview first: nothing changes until you copy."
      />
      <Paper withBorder p="lg">
        <Stack gap="md">
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
            <Select
              label="Copy from"
              data={companies.map((c) => ({ value: c.id, label: c.nameEn }))}
              value={sourceCompanyId}
              onChange={(v) => {
                setSource(v);
                setTargets((t) => t.filter((x) => x !== v));
              }}
              allowDeselect={false}
            />
            <MultiSelect
              label="Copy to"
              placeholder={targetCompanyIds.length ? '' : 'Choose companies'}
              data={companies.filter((c) => c.id !== sourceCompanyId).map((c) => ({ value: c.id, label: c.nameEn }))}
              value={targetCompanyIds}
              onChange={setTargets}
            />
          </SimpleGrid>
          <div>
            <Group justify="space-between" mb={6}>
              <Text size="sm" fw={500}>
                Sections
              </Text>
              <Button
                size="compact-xs"
                variant="subtle"
                onClick={() => setSections(sections.length === allSections.length ? [] : allSections.map((s) => s.key))}
              >
                {sections.length === allSections.length && allSections.length ? 'Clear' : 'Choose all'}
              </Button>
            </Group>
            <Checkbox.Group value={sections} onChange={setSections}>
              <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing={8}>
                {allSections.map((s) => (
                  <Checkbox key={s.key} value={s.key} label={s.label} />
                ))}
              </SimpleGrid>
            </Checkbox.Group>
          </div>
          <Radio.Group
            label="When the target already has it with different values"
            value={onConflict}
            onChange={setOnConflict}
          >
            <Group mt={6}>
              <Radio value="skip" label="Keep the target's version" />
              <Radio value="overwrite" label="Overwrite with the source's version" />
            </Group>
          </Radio.Group>
          <Text size="xs" c="dimmed">
            Anything that names people, branches or departments of the source company (approval steps naming a person,
            conditional flows, branch holidays) is never copied; statutory components are never overwritten.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" leftSection={<IconEye size={16} stroke={1.6} />} disabled={!ready} loading={busy} onClick={preview}>
              Preview
            </Button>
            <Button
              leftSection={<IconCopy size={16} stroke={1.6} />}
              disabled={!plan || applied || !previewChanges(plan.totals)}
              onClick={() => setConfirm(true)}
            >
              Copy
            </Button>
          </Group>
        </Stack>
      </Paper>
      {plan ? (
        <div>
          <SectionTitle>{applied ? 'Result' : 'Preview'}</SectionTitle>
          <PlanView plan={plan} applied={applied} />
        </div>
      ) : null}
      <ConfirmDialog
        opened={confirm}
        title="Copy these policies?"
        color={onConflict === 'overwrite' ? 'orange' : 'brand'}
        message={plan ? `${policyTotalsLabel(plan.totals)}. Every row written is recorded in the audit log of the target company.` : ''}
        confirmLabel="Copy"
        onClose={() => setConfirm(false)}
        onConfirm={apply}
      />
    </Stack>
  );
}
