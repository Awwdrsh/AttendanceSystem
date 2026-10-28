import { Badge, Button, Group, Modal, SegmentedControl, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { compliance } from '../../../api/compliance';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { DataTable } from '../../../components/DataTable';
import { PageHeader } from '../../../components/PageHeader';
import { bsMonthLabel, fmtAd, fmtMoney } from '../../../lib/format';
import { toast } from '../../../lib/toast';
import { useQuery } from '../../../lib/useQuery';

const STATUS = { held: 'On hold', releasing: 'Paid with next run', released: 'Released', cancelled: 'Cancelled' };
const COLOR = { held: 'orange', releasing: 'blue', released: 'teal', cancelled: 'gray' };

/** HR: pay held back from a run (pending documents, an investigation) and releasing it. */
export default function HeldPayPage() {
  const [tab, setTab] = useState('open');
  const q = useQuery(() => compliance.holds(tab), [tab]);
  const [rel, setRel] = useState(null);
  const [mode, setMode] = useState('payroll');
  const [note, setNote] = useState('');
  const [cancel, setCancel] = useState(null);
  const [busy, setBusy] = useState(false);
  const release = async () => {
    if (!rel) return;
    setBusy(true);
    try {
      await compliance.release(rel.id, mode, note.trim() || undefined);
      toast.ok(mode === 'payroll' ? `${rel.name_en}'s held pay will be paid in the next pay run.` : `Recorded as paid outside payroll.`);
      setRel(null);
      setNote('');
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="lg">
      <PageHeader
        title="Held pay"
        subtitle="Net pay held back in a run is paid later: with the next pay run as a separate line (not taxed again), or recorded as paid outside payroll. Hold someone's pay from the pay run's review step."
        actions={
          <SegmentedControl
            value={tab}
            onChange={(v) => setTab(v)}
            data={[
              { value: 'open', label: 'Open' },
              { value: 'released', label: 'Released' },
              { value: 'cancelled', label: 'Cancelled' },
            ]}
          />
        }
      />
      <DataTable
        rows={q.data?.holds ?? []}
        rowKey={(h) => h.id}
        loading={q.loading}
        error={q.error}
        emptyTitle={tab === 'open' ? 'No pay on hold' : 'Nothing here'}
        {...(tab === 'open'
          ? { emptyHint: "Everyone is being paid. To hold someone's pay, open a pay run and use Hold pay on the review step." }
          : {})}
        columns={[
          {
            key: 'emp',
            header: 'Employee',
            render: (h) => (
              <>
                <Text size="sm" fw={500}>
                  {h.name_en}
                </Text>
                <Text size="xs" c="dimmed">
                  {h.emp_code}
                </Text>
              </>
            ),
          },
          {
            key: 'run',
            header: 'Held in',
            render: (h) => <Link to={`/payroll/runs/${h.run_id}`}>{bsMonthLabel(h.bs_year, h.bs_month)}</Link>,
          },
          {
            key: 'amount',
            header: 'Net held',
            align: 'right',
            render: (h) => (
              <Text size="sm" fw={600} className="num">
                {fmtMoney(h.amount)}
              </Text>
            ),
          },
          {
            key: 'reason',
            header: 'Reason',
            render: (h) => (
              <>
                <Text size="sm" lineClamp={2}>
                  {h.reason}
                </Text>
                <Text size="xs" c="dimmed">
                  {h.held_by_name ?? ''} · {fmtAd(h.held_at.slice(0, 10))}
                </Text>
              </>
            ),
          },
          {
            key: 'status',
            header: 'Status',
            render: (h) => (
              <>
                <Badge variant="light" color={COLOR[h.status]}>
                  {STATUS[h.status]}
                </Badge>
                {h.release_bs_year ? (
                  <Text size="xs" c="dimmed">
                    in {bsMonthLabel(h.release_bs_year, h.release_bs_month)}
                  </Text>
                ) : null}
              </>
            ),
          },
          {
            key: 'act',
            header: '',
            align: 'right',
            render: (h) =>
              h.status !== 'held' ? null : (
                <Group gap={6} justify="flex-end" wrap="nowrap">
                  {h.run_status === 'locked' ? (
                    <Button
                      size="xs"
                      variant="light"
                      onClick={() => {
                        setRel(h);
                        setMode('payroll');
                        setNote('');
                      }}
                    >
                      Release
                    </Button>
                  ) : (
                    <Button size="xs" variant="subtle" color="gray" onClick={() => setCancel(h)}>
                      Undo hold
                    </Button>
                  )}
                </Group>
              ),
          },
        ]}
      />
      <Modal opened={!!rel} onClose={() => setRel(null)} title={`Release ${rel?.name_en ?? ''}'s pay`} centered>
        <Stack>
          <Text size="sm">
            NPR <b className="num">{fmtMoney(rel?.amount, { currency: false })}</b> held in{' '}
            {rel ? bsMonthLabel(rel.bs_year, rel.bs_month) : ''}.
          </Text>
          <SegmentedControl
            value={mode}
            onChange={(v) => setMode(v)}
            fullWidth
            data={[
              { value: 'payroll', label: 'Pay with next run' },
              { value: 'outside', label: 'Already paid outside' },
            ]}
          />
          <Text size="xs" c="dimmed">
            {mode === 'payroll'
              ? 'Added to the next regular, supplementary or off-cycle run as "Released pay". Tax was already counted when the pay was held.'
              : 'Use this when HR has paid the amount another way (for example by cheque). Nothing is added to payroll.'}
          </Text>
          <TextInput label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} placeholder="Optional" />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setRel(null)}>
              Cancel
            </Button>
            <Button loading={busy} onClick={release}>
              Release
            </Button>
          </Group>
        </Stack>
      </Modal>
      <ConfirmDialog
        opened={!!cancel}
        onClose={() => setCancel(null)}
        title="Undo this hold?"
        confirmLabel="Undo hold"
        color="brand"
        message={`${cancel?.name_en ?? ''} will be paid normally in this run.`}
        onConfirm={async () => {
          if (!cancel) return;
          try {
            await compliance.cancelHold(cancel.id);
            toast.ok('Hold removed.');
            await q.reload();
          } catch (e) {
            toast.err(e);
          }
        }}
      />
    </Stack>
  );
}
