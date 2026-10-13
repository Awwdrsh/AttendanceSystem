import { Badge, Button, Code, Group, Modal, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { payroll as payApi } from '../../api';
import type { RuleVersion } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Versioned statutory rules (plan §6.13): every rate and slab is a dated row; payslips snapshot the IDs they used. */
export default function RulesPage() {
  const { t } = useI18n();
  const { hasRole } = useAuth();
  const q = useQuery(() => payApi.rules(), []);
  const [view, setView] = useState<RuleVersion | null>(null);
  return (
    <Stack>
      <PageHeader title={t('statutoryRules')} subtitle="Resolved as of each pay period's dates. New fiscal-year schedules are added as new rows each Ashadh (plan §16.6); nothing is overwritten." />
      <DataTable<RuleVersion> rows={q.data?.rules ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} emptyTitle="No rules seeded." emptyHint="Run the defaults seed from Setup." onRowClick={setView}
        columns={[
          { key: 'key', header: 'Rule', render: (r) => <Group gap={4}><Text size="sm" fw={500}>{r.rule_key}</Text>{r.locked ? <Badge size="xs" variant="light" color="gray">🔒 statutory</Badge> : null}</Group> },
          { key: 'valid', header: 'Valid', render: (r) => `${r.valid_from_bs} → ${r.valid_to_bs ?? 'open'}` }, { key: 'ad', header: 'AD', render: (r) => `${r.valid_from_ad} → ${r.valid_to_ad ?? ''}` },
          { key: 'approved', header: 'Approved', render: (r) => r.approved_at ? new Date(r.approved_at).toLocaleDateString() : <Badge size="xs" variant="light" color="orange">pending</Badge> },
          { key: 'src', header: 'Source', render: (r) => r.source_url ? <a href={r.source_url} target="_blank" rel="noreferrer">link</a> : '—' },
          { key: 'a', header: '', render: (r) => !r.approved_at && hasRole('finance') ? <Button size="xs" variant="light" onClick={async (e) => { e.stopPropagation(); try { await payApi.approveRule(r.id); toast.ok('Rule approved.'); await q.reload(); } catch (err) { toast.err(err); } }}>{t('approve')}</Button> : null },
        ]} />
      <Modal opened={!!view} onClose={() => setView(null)} title={view?.rule_key} size="lg" centered>
        {view ? <Stack gap="xs"><Text size="sm">{view.note}</Text><Code block style={{ maxHeight: 400, overflow: 'auto' }}>{JSON.stringify(view.value, null, 2)}</Code></Stack> : null}
      </Modal>
    </Stack>
  );
}
