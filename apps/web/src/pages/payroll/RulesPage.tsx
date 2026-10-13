import { Anchor, Badge, Button, Code, Group, Modal, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconCheck, IconExternalLink, IconLock } from '@tabler/icons-react';
import { useState } from 'react';
import { payroll as payApi } from '../../api';
import type { RuleVersion } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { Field } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const effective = (r: RuleVersion) => r.valid_to_ad ? `${fmtAd(r.valid_from_ad)} to ${fmtAd(r.valid_to_ad)}` : `From ${fmtAd(r.valid_from_ad)}`;

/** Versioned statutory rules: every rate and slab is a dated row; payslips record the versions they used. */
export default function RulesPage() {
  const { hasRole } = useAuth();
  const q = useQuery(() => payApi.rules(), []);
  const [view, setView] = useState<RuleVersion | null>(null);
  return (
    <Stack gap="lg">
      <PageHeader title="Statutory rules" subtitle="Tax slabs, SSF rates and other legal values, each valid for a date range. Every pay run uses the version in force for its period; new fiscal-year values are added as new rows, never overwritten." />
      <DataTable<RuleVersion> rows={q.data?.rules ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={setView}
        emptyTitle="No rules loaded" emptyHint="Load the default statutory rules from Setup, then approve them here."
        columns={[
          { key: 'key', header: 'Rule', render: (r) => <Group gap={6} wrap="nowrap"><Text size="sm" fw={500} ff="monospace">{r.rule_key}</Text>{r.locked ? <Badge size="sm" variant="light" color="gray" leftSection={<IconLock size={12} stroke={1.6} />}>Statutory</Badge> : null}</Group> },
          { key: 'valid', header: 'Effective', render: (r) => <Text size="sm" c="dimmed">{effective(r)}</Text> },
          { key: 'approved', header: 'Approval', render: (r) => r.approved_at ? <Text size="sm" c="dimmed">{fmtAd(r.approved_at)}</Text> : <StatusBadge status="pending" /> },
          { key: 'src', header: 'Source', render: (r) => r.source_url ? <Anchor href={r.source_url} target="_blank" rel="noreferrer" size="sm" onClick={(e) => e.stopPropagation()} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>Open<IconExternalLink size={14} stroke={1.6} /></Anchor> : <Text size="sm" c="dimmed">—</Text> },
          { key: 'a', header: '', align: 'right', render: (r) => !r.approved_at && hasRole('finance') ? <Button size="xs" variant="default" leftSection={<IconCheck size={14} stroke={1.6} />} onClick={async (e) => { e.stopPropagation(); try { await payApi.approveRule(r.id); toast.ok('Rule approved.'); await q.reload(); } catch (err) { toast.err(err); } }}>Approve</Button> : null },
        ]} />
      <Modal opened={!!view} onClose={() => setView(null)} title={<Text fw={600} ff="monospace">{view?.rule_key}</Text>} size="lg" centered>
        {view ? <Stack gap="md">
          {view.note ? <Text size="sm">{view.note}</Text> : null}
          <SimpleGrid cols={{ base: 1, sm: 3 }}>
            <Field label="Effective">{effective(view)}</Field>
            <Field label="Approved">{view.approved_at ? fmtAd(view.approved_at) : 'Not yet'}</Field>
            <Field label="Jurisdiction">{view.jurisdiction}</Field>
          </SimpleGrid>
          <Code block style={{ maxHeight: 400, overflow: 'auto' }}>{JSON.stringify(view.value, null, 2)}</Code>
        </Stack> : null}
      </Modal>
    </Stack>
  );
}
