import { Button, Group, Modal, NumberInput, SegmentedControl, Select, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconBooks, IconPlus, IconTargetArrow } from '@tabler/icons-react';
import { useState } from 'react';
import { performance as perfApi } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { GoalCard, GoalDetail, GoalDrawer, ICON } from './parts';

/** My goals and the company and department goals they can support. */
export default function MyGoalsPage() {
  const [view, setView] = useState('mine');
  const [status, setStatus] = useState('open');
  const q = useQuery(() => perfApi.goals({ scope: view }), [view]);
  const [create, setCreate] = useState(false);
  const [openId, setOpenId] = useState(null);
  const [lib, setLib] = useState(false);
  const all = q.data?.goals ?? [];
  const goals =
    status === 'open' ? all.filter((g) => g.status === 'active' || g.status === 'draft') : all.filter((g) => g.status !== 'cancelled');
  const mine = view === 'mine' ? all.filter((g) => g.status === 'active') : [];
  const avg = mine.length ? Math.round(mine.reduce((s, g) => s + Number(g.progress), 0) / mine.length) : 0;
  const weight = mine.reduce((s, g) => s + Number(g.weight), 0);

  return (
    <Stack gap="lg">
      <SectionTitle
        hint="Set goals with measurable key results and check in as you go. Your manager sees your goals and updates."
        actions={
          <Group gap="xs" wrap="wrap">
            <SegmentedControl
              size="xs"
              value={view}
              onChange={(v) => setView(v)}
              data={[
                { value: 'mine', label: 'My goals' },
                { value: 'company', label: 'Company goals' },
              ]}
            />
            <SegmentedControl
              size="xs"
              value={status}
              onChange={(v) => setStatus(v)}
              data={[
                { value: 'open', label: 'Open' },
                { value: 'all', label: 'All' },
              ]}
            />
            {view === 'mine' ? (
              <>
                <Button size="xs" variant="default" leftSection={<IconBooks {...ICON} />} onClick={() => setLib(true)}>
                  From KPI library
                </Button>
                <Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setCreate(true)}>
                  New goal
                </Button>
              </>
            ) : null}
          </Group>
        }
      >
        Goals
      </SectionTitle>
      {view === 'mine' && mine.length ? (
        <StatStrip
          stats={[
            { key: 'n', label: 'Active goals', value: mine.length },
            { key: 'p', label: 'Average progress', value: `${avg}%` },
            {
              key: 'w',
              label: 'Total weight',
              value: `${weight}%`,
              ...(weight > 0 && weight !== 100 ? { hint: 'Weights usually add up to 100%', tone: 'warn' } : {}),
            },
          ]}
        />
      ) : null}
      {!q.loading && goals.length === 0 ? (
        <EmptyState
          icon={<IconTargetArrow size={20} stroke={1.6} />}
          title={view === 'mine' ? 'No goals yet' : 'No company goals yet'}
          hint={
            view === 'mine'
              ? 'Write two to five goals for this year, each with a way to measure it.'
              : 'HR publishes company and department goals here.'
          }
          {...(view === 'mine' ? { action: { label: 'New goal', onClick: () => setCreate(true) } } : {})}
        />
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
          {goals.map((g) => (
            <GoalCard key={g.id} g={g} onOpen={(x) => setOpenId(x.id)} />
          ))}
        </SimpleGrid>
      )}
      <GoalDrawer opened={create} onClose={() => setCreate(false)} onSaved={() => void q.reload()} />
      <GoalDetail goalId={openId} onClose={() => setOpenId(null)} onChanged={() => void q.reload()} />
      <KpiLibraryModal opened={lib} onClose={() => setLib(false)} onAdded={() => void q.reload()} />
    </Stack>
  );
}

/** Pick a KPI from HR's library and turn it into a goal. */
export function KpiLibraryModal({ opened, onClose, onAdded, employeeId }) {
  const q = useQuery(() => perfApi.kpis(), [opened], { enabled: opened });
  const [kpiId, setKpiId] = useState(null);
  const [target, setTarget] = useState('');
  const [weight, setWeight] = useState('');
  const [due, setDue] = useState(null);
  const [busy, setBusy] = useState(false);
  const kpi = q.data?.kpis.find((k) => k.id === kpiId);
  const add = async () => {
    if (!kpiId) return;
    setBusy(true);
    try {
      await perfApi.applyKpi(kpiId, {
        ...(employeeId ? { employeeId } : {}),
        ...(target !== '' ? { target } : {}),
        weight: weight === '' ? 0 : weight,
        dueDate: due,
      });
      toast.ok('Goal added.');
      setKpiId(null);
      onAdded();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title="Add from the KPI library">
      <Stack gap="sm">
        {q.data && q.data.kpis.length === 0 ? (
          <Text size="sm" c="dimmed">
            HR has not added KPIs yet.
          </Text>
        ) : (
          <Select
            label="KPI"
            searchable
            value={kpiId}
            onChange={(v) => {
              setKpiId(v);
              setTarget('');
            }}
            data={(q.data?.kpis ?? []).map((k) => ({ value: k.id, label: `${k.title}${k.designation ? ` (${k.designation})` : ''}` }))}
          />
        )}
        {kpi?.description ? (
          <Text size="sm" c="dimmed">
            {kpi.description}
          </Text>
        ) : null}
        <SimpleGrid cols={2}>
          <NumberInput
            label="Target"
            placeholder={kpi ? String(Number(kpi.default_target)) : ''}
            value={target}
            onChange={(v) => setTarget(v === '' ? '' : Number(v))}
          />
          <NumberInput label="Weight (%)" min={0} max={100} value={weight} onChange={(v) => setWeight(v === '' ? '' : Number(v))} />
        </SimpleGrid>
        <DateField label="Due date" value={due} onChange={setDue} />
        <Group justify="flex-end">
          <Button onClick={add} loading={busy} disabled={!kpiId}>
            Add goal
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
