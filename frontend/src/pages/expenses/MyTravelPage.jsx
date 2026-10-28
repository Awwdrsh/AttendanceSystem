import { Button, Drawer, Stack, Text } from '@mantine/core';
import { IconPlane, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { expenses as api } from '../../api/expenses';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';
import { DEST_LABEL, money, span, TravelForm, TripBadge, TripDrawer } from './shared';

/** Employee travel: request a trip with its daily allowance and advance, then settle it with the bills afterwards. */
export default function MyTravelPage() {
  const policy = useQuery(() => api.policy(), []);
  const q = useQuery(() => api.trips({ scope: 'own' }), []);
  const [form, setForm] = useState(false);
  const [open, setOpen] = useState(null);
  const rows = q.data?.trips ?? [];
  const today = todayNpt();
  const toSettle = rows.filter((t) => t.status === 'approved' && t.fromDate <= today);
  const upcoming = rows.filter((t) => (t.status === 'approved' || t.status === 'pending') && t.fromDate > today);
  const advanceOut = rows
    .filter((t) => t.advanceStatus !== 'none' && (t.status === 'approved' || t.status === 'settling'))
    .reduce((a, t) => a + Number(t.advanceApproved ?? 0), 0);

  return (
    <Stack gap="lg">
      <SectionTitle
        hint="Ask before you travel. The daily allowance follows your grade and destination; settle the trip with your bills when you are back."
        actions={
          <Button leftSection={<IconPlus size={16} />} onClick={() => setForm(true)} disabled={!policy.data}>
            Request travel
          </Button>
        }
      >
        Travel
      </SectionTitle>
      <StatStrip
        stats={[
          { key: 'u', label: 'Upcoming trips', value: upcoming.length },
          {
            key: 's',
            label: 'To settle',
            value: toSettle.length,
            ...(toSettle.length ? { tone: 'warn', hint: 'Open the trip and add your bills' } : {}),
          },
          { key: 'a', label: 'Advance not yet settled', value: money(advanceOut) },
        ]}
      />
      {!q.loading && rows.length === 0 ? (
        <EmptyState
          icon={<IconPlane size={20} stroke={1.6} />}
          title="No trips yet"
          hint="Travelling for work? Request it here to get the daily allowance and, if you need it, an advance."
          {...(policy.data ? { action: { label: 'Request travel', onClick: () => setForm(true) } } : {})}
        />
      ) : (
        <DataTable
          rows={rows}
          rowKey={(t) => t.id}
          loading={q.loading}
          error={q.error}
          onRowClick={(t) => setOpen(t.id)}
          highlight={(t) => t.status === 'approved' && t.fromDate <= today}
          columns={[
            {
              key: 'd',
              header: 'Trip',
              render: (t) => (
                <>
                  <Text size="sm" fw={500}>
                    {t.destination}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {DEST_LABEL[t.destinationCategory]} · {span(t.fromDate, t.toDate)}
                  </Text>
                </>
              ),
            },
            { key: 'a', header: 'Allowance', align: 'right', render: (t) => money(t.daAmount) },
            {
              key: 'v',
              header: 'Advance',
              align: 'right',
              render: (t) => (Number(t.advanceRequested) > 0 ? money(t.advanceApproved ?? t.advanceRequested) : '—'),
            },
            { key: 's', header: 'Status', render: (t) => <TripBadge status={t.status} /> },
          ]}
        />
      )}
      <Drawer opened={form} onClose={() => setForm(false)} position="right" size="lg" title={<Text fw={600}>Request travel</Text>}>
        {form && policy.data ? (
          <TravelForm
            policy={policy.data}
            onDone={(id) => {
              setForm(false);
              void q.reload();
              setOpen(id);
            }}
          />
        ) : null}
      </Drawer>
      <TripDrawer id={open} onClose={() => setOpen(null)} onChanged={() => void q.reload()} />
    </Stack>
  );
}
