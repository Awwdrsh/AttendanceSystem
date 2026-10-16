import { Badge, Button, Group, Modal, NumberInput, Paper, SegmentedControl, Stack, TagsInput, Text } from '@mantine/core';
import { IconSettings } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { relations, type Milestone, type RelationsPolicy } from '../../api/relations';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const KIND: Record<Milestone['kind'], { label: string; color: string }> = {
  service: { label: 'Service', color: 'teal' }, contract_end: { label: 'Contract end', color: 'orange' }, retirement: { label: 'Retirement', color: 'grape' },
  probation_end: { label: 'Probation', color: 'blue' }, assignment_end: { label: 'Temporary role', color: 'cyan' }, exit: { label: 'Leaving', color: 'red' },
};

function PolicyModal({ opened, onClose, policy, onSaved }: { opened: boolean; onClose: () => void; policy: RelationsPolicy | null; onSaved: () => void }) {
  const [p, setP] = useState<RelationsPolicy | null>(policy);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setP(policy); }, [policy, opened]);
  if (!p) return null;
  return (
    <Modal opened={opened} onClose={onClose} title="Milestone and relations settings" centered>
      <Stack>
        <NumberInput label="Retirement age" description="Retirement date = date of birth plus this many years." min={40} max={80} value={p.retirementAge} onChange={(v) => setP({ ...p, retirementAge: Number(v) || 58 })} />
        <TagsInput label="Service years to celebrate" description="Type a number and press Enter." value={p.serviceYears.map(String)}
          onChange={(v) => setP({ ...p, serviceYears: v.map(Number).filter((n) => Number.isInteger(n) && n > 0 && n <= 60) })} />
        <NumberInput label="Days to appeal a grievance decision" min={1} max={90} value={p.appealDays} onChange={(v) => setP({ ...p, appealDays: Number(v) || 15 })} />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} onClick={async () => { setBusy(true); try { await relations.savePolicy(p); toast.ok('Saved.'); onSaved(); onClose(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Save</Button></Group>
      </Stack>
    </Modal>
  );
}

/** HR: what is coming up in people's careers: service anniversaries, contract ends, retirements, probation ends and exits. */
export default function MilestonesPage() {
  const [days, setDays] = useState('60');
  const q = useQuery(() => relations.milestones(Number(days)), [days]);
  const [editing, setEditing] = useState(false);
  const list = q.data?.milestones ?? [];
  const count = (k: Milestone['kind']) => list.filter((m) => m.kind === k).length;
  const today = todayNpt();
  return (
    <Stack gap="lg">
      <PageHeader title="Milestones" subtitle="Upcoming dates to plan for. HR and the manager get a reminder 30 and 7 days before a contract end or retirement, and on service anniversaries."
        actions={<><SegmentedControl value={days} onChange={setDays} data={[{ value: '30', label: '30 days' }, { value: '60', label: '60 days' }, { value: '180', label: '6 months' }, { value: '366', label: '1 year' }]} />
          <Button variant="default" leftSection={<IconSettings size={16} stroke={1.6} />} onClick={() => setEditing(true)}>Settings</Button></>} />
      <StatStrip stats={[
        { key: 's', label: 'Service anniversaries', value: count('service') },
        { key: 'c', label: 'Contracts ending', value: count('contract_end') },
        { key: 'r', label: 'Retirements', value: count('retirement') },
        { key: 'e', label: 'Leaving', value: count('exit') },
      ]} />
      <Paper withBorder p={0}>
        <DataTable<Milestone> rows={list} rowKey={(r) => `${r.kind}:${r.employeeId}:${r.date}`} loading={q.loading} error={q.error}
          emptyTitle="Nothing coming up" emptyHint="Set contract end dates on the employee's Relations tab; retirements come from the date of birth."
          columns={[
            { key: 'd', header: 'Date', render: (r) => <Text size="sm" {...(r.date < today ? { c: 'red' } : {})}>{fmtAd(r.date)}</Text> },
            { key: 'k', header: 'What', render: (r) => <Badge variant="light" color={KIND[r.kind].color}>{KIND[r.kind].label}</Badge> },
            { key: 'e', header: 'Employee', render: (r) => <Link to={`/employees/${r.employeeId}`} style={{ color: 'inherit' }}><Text size="sm">{r.name}</Text><Text size="xs" c="dimmed">{r.empCode}</Text></Link> },
            { key: 't', header: 'Detail', render: (r) => r.detail },
          ]} />
      </Paper>
      <PolicyModal opened={editing} onClose={() => setEditing(false)} policy={q.data?.policy ?? null} onSaved={() => { void q.reload(); }} />
    </Stack>
  );
}
