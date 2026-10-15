import { ActionIcon, Button, Group, Menu, Modal, NumberInput, Paper, Progress, SegmentedControl, SimpleGrid, Stack, Switch, Table, Text } from '@mantine/core';
import { IconCopy, IconDots, IconExternalLink, IconPlus, IconSettings } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { EMPLOYMENT, hiring, SOURCE_LABEL, WORK_MODE, type HiringPolicy, type Job } from '../../api/hiring';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { JobForm } from './JobForm';
import { ICON, JobStatusBadge } from './shared';

function careersUrl(jobId?: string, code?: string | null): string {
  const base = `${window.location.origin}/careers${jobId ? `/${jobId}` : ''}`;
  return code ? `${base}?company=${encodeURIComponent(code)}` : base;
}

async function copy(text: string, what: string) {
  try { await navigator.clipboard.writeText(text); toast.ok(`${what} copied.`); } catch { toast.info(text, 'Copy this link'); }
}

function PolicyModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const q = useQuery(() => hiring.policy(), [opened], { enabled: opened });
  const [p, setP] = useState<HiringPolicy>({ retentionDays: 180, careersEnabled: true });
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (q.data) setP(q.data.policy); }, [q.data]);
  const save = async () => {
    setBusy(true);
    try { await hiring.setPolicy(p); toast.ok('Hiring settings saved.'); onClose(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Hiring settings</Text>}>
      <Stack gap="md">
        <Switch label="Public careers page" description="Open jobs are listed at /careers and anyone can apply." checked={p.careersEnabled} onChange={(e) => setP({ ...p, careersEnabled: e.currentTarget.checked })} />
        <NumberInput label="Keep data of candidates who were not selected for (days)" description="After this, their name, contact details, CV and answers are deleted automatically. Hired candidates are kept."
          min={30} max={1095} value={p.retentionDays} onChange={(v) => setP({ ...p, retentionDays: Number(v) || 180 })} />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button onClick={save} loading={busy}>Save</Button></Group>
      </Stack>
    </Modal>
  );
}

function Reports() {
  const r = useQuery(() => hiring.reports(), []);
  const d = r.data;
  if (!d) return null;
  return (
    <Stack gap="lg">
      <StatStrip stats={[
        { key: 'a', label: 'Applications', value: d.totals.applications },
        { key: 'p', label: 'In progress', value: d.totals.active },
        { key: 'h', label: 'Hired', value: d.totals.hired },
        { key: 't', label: 'Average time to hire', value: d.timeToHire.averageDays === null ? '—' : `${d.timeToHire.averageDays} days`, hint: d.timeToHire.medianDays === null ? undefined : `Median ${d.timeToHire.medianDays} days` },
      ]} />
      {d.funnel.length ? (
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
          <Paper withBorder radius="lg" p="md">
            <SectionTitle hint="How many applicants reached each stage">Funnel by job</SectionTitle>
            <Stack gap="md">
              {d.funnel.slice(0, 5).map((f) => (
                <div key={f.jobId}>
                  <Text size="sm" fw={500} mb={4}>{f.title}</Text>
                  <Stack gap={3}>
                    {f.stages.map((s) => (
                      <Group key={s.stage} gap="xs" wrap="nowrap">
                        <Text size="xs" w={80} c="dimmed" truncate>{s.stage}</Text>
                        <Progress style={{ flex: 1 }} value={f.total ? (100 * s.reached) / f.total : 0} size="md" radius="sm" />
                        <Text size="xs" w={70} ta="right" className="num">{s.reached}{f.total ? ` (${Math.round((100 * s.reached) / f.total)}%)` : ''}</Text>
                      </Group>
                    ))}
                  </Stack>
                </div>
              ))}
            </Stack>
          </Paper>
          <Paper withBorder radius="lg" p="md">
            <SectionTitle hint="Where applicants came from and who was hired">Sources</SectionTitle>
            <Table>
              <Table.Thead><Table.Tr><Table.Th>Source</Table.Th><Table.Th ta="right">Applications</Table.Th><Table.Th ta="right">Hired</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{d.sources.map((s) => <Table.Tr key={s.source}><Table.Td>{SOURCE_LABEL[s.source] ?? s.source}</Table.Td><Table.Td ta="right" className="num">{s.applications}</Table.Td><Table.Td ta="right" className="num">{s.hired}</Table.Td></Table.Tr>)}</Table.Tbody>
            </Table>
          </Paper>
        </SimpleGrid>
      ) : null}
    </Stack>
  );
}

/** Job openings: draft, publish to the careers page, share, close; HR also sees the hiring funnel. */
export default function JobsPage() {
  const { hasRole } = useAuth();
  const hr = hasRole('hr');
  const nav = useNavigate();
  const [status, setStatus] = useState('all');
  const q = useQuery(() => hiring.jobs(status === 'all' ? undefined : status), [status]);
  const [edit, setEdit] = useState<Job | null | undefined>(undefined);
  const [settings, setSettings] = useState(false);
  const code = q.data?.companyCode ?? null;

  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); toast.ok(ok); await q.reload(); } catch (e) { toast.err(e); } };

  return (
    <Stack gap="lg">
      <PageHeader title="Jobs" subtitle="Open a role, publish it to the careers page, and follow every candidate through to hire."
        {...(hr ? { actions: <>
          <Button variant="default" leftSection={<IconExternalLink {...ICON} />} onClick={() => window.open(careersUrl(undefined, code), '_blank', 'noopener')}>Careers page</Button>
          <ActionIcon variant="default" size="lg" aria-label="Hiring settings" onClick={() => setSettings(true)}><IconSettings {...ICON} /></ActionIcon>
          <Button leftSection={<IconPlus {...ICON} />} onClick={() => setEdit(null)}>New job</Button>
        </> } : {})} />
      {hr ? <Reports /> : null}
      <SegmentedControl w="fit-content" value={status} onChange={setStatus} data={[{ value: 'all', label: 'All' }, { value: 'open', label: 'Open' }, { value: 'draft', label: 'Drafts' }, { value: 'closed', label: 'Closed' }]} />
      <DataTable<Job> rows={q.data?.jobs ?? []} rowKey={(j) => j.id} loading={q.loading} error={q.error}
        emptyTitle="No jobs here" emptyHint={hr ? 'Create a job, or turn an approved hiring request into one.' : 'Jobs for your department appear here once HR creates them.'}
        onRowClick={(j) => nav(`/hiring/pipeline?job=${j.id}`)}
        columns={[
          { key: 'title', header: 'Job', render: (j) => <div><Text size="sm" fw={500}>{j.title}</Text><Text size="xs" c="dimmed">{[j.department, j.location, WORK_MODE[j.work_mode], EMPLOYMENT[j.employment_type]].filter(Boolean).join(' · ')}</Text></div> },
          { key: 'status', header: 'Status', render: (j) => <JobStatusBadge status={j.status} /> },
          { key: 'apps', header: 'Candidates', align: 'right', render: (j) => <div><Text size="sm" className="num">{j.active ?? 0} active</Text><Text size="xs" c="dimmed">{j.applications ?? 0} total{j.new_this_week ? `, ${j.new_this_week} new this week` : ''}</Text></div> },
          { key: 'deadline', header: 'Apply by', render: (j) => <Text size="sm" c={j.deadline && j.deadline < todayNpt() && j.status === 'open' ? 'red' : 'inherit'}>{j.deadline ? fmtDateShort(j.deadline) : 'No deadline'}</Text> },
          { key: 'manager', header: 'Hiring manager', render: (j) => <Text size="sm">{j.hiring_manager_name ?? '—'}</Text> },
          {
            key: 'actions', header: '', align: 'right', render: (j) => (
              <Group gap={4} justify="flex-end" wrap="nowrap" onClick={(e) => e.stopPropagation()}>
                {j.status === 'open' ? <ActionIcon variant="subtle" color="gray" aria-label="Copy link to this job" onClick={() => copy(careersUrl(j.id, code), 'Job link')}><IconCopy {...ICON} /></ActionIcon> : null}
                {hr ? (
                  <Menu position="bottom-end" withinPortal>
                    <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label={`Actions for ${j.title}`}><IconDots {...ICON} /></ActionIcon></Menu.Target>
                    <Menu.Dropdown>
                      <Menu.Item onClick={() => setEdit(j)}>Edit</Menu.Item>
                      {j.status !== 'open' ? <Menu.Item onClick={() => act(() => hiring.publishJob(j.id), 'Published. The job is on the careers page.')}>Publish</Menu.Item> : null}
                      {j.status === 'open' ? <Menu.Item onClick={() => window.open(careersUrl(j.id, code), '_blank', 'noopener')}>View on careers page</Menu.Item> : null}
                      {j.status === 'open' ? <Menu.Item color="red" onClick={() => act(() => hiring.closeJob(j.id), 'Closed. No new applications will be accepted.')}>Close</Menu.Item> : null}
                    </Menu.Dropdown>
                  </Menu>
                ) : null}
              </Group>
            ),
          },
        ]} />
      <JobForm opened={edit !== undefined} job={edit ?? null} onClose={() => setEdit(undefined)} onSaved={() => { setEdit(undefined); void q.reload(); }} />
      <PolicyModal opened={settings} onClose={() => setSettings(false)} />
    </Stack>
  );
}
