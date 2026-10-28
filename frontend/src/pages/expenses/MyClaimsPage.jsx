import { Button, Drawer, Stack, Text } from '@mantine/core';
import { IconPlus, IconReceiptRefund } from '@tabler/icons-react';
import { useState } from 'react';
import { expenses as api } from '../../api/expenses';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { useQuery } from '../../lib/useQuery';
import { ClaimBadge, ClaimDrawer, ClaimForm, money, payoutLabel, span } from './shared';

/** Employee expense claims: file one with receipts, follow it through approval and into the salary. */
export default function MyClaimsPage() {
  const policy = useQuery(() => api.policy(), []);
  const q = useQuery(() => api.claims({ scope: 'own' }), []);
  const [form, setForm] = useState(null);
  const [open, setOpen] = useState(null);
  const rows = q.data?.claims ?? [];
  const sum = (f, v) => rows.filter(f).reduce((a, c) => a + Number(v(c) ?? 0), 0);
  const waiting = rows.filter((c) => c.status === 'pending');
  const toBePaid = sum(
    (c) => c.status === 'approved',
    (c) => c.totalApproved,
  );
  const paid = sum(
    (c) => c.status === 'paid',
    (c) => c.totalApproved,
  );

  return (
    <Stack gap="lg">
      <SectionTitle
        hint="Claim money you spent for work. Approved claims are paid with your next salary and are not taxed."
        actions={
          <Button leftSection={<IconPlus size={16} />} onClick={() => setForm({ claim: null })} disabled={!policy.data}>
            New claim
          </Button>
        }
      >
        Expense claims
      </SectionTitle>
      <StatStrip
        stats={[
          {
            key: 'w',
            label: 'Waiting for approval',
            value: waiting.length,
            hint: waiting.length
              ? money(
                  sum(
                    (c) => c.status === 'pending',
                    (c) => c.totalClaimed,
                  ),
                )
              : undefined,
          },
          { key: 'a', label: 'Approved, to be paid', value: money(toBePaid) },
          { key: 'p', label: 'Paid so far', value: money(paid) },
        ]}
      />
      {!q.loading && rows.length === 0 ? (
        <EmptyState
          icon={<IconReceiptRefund size={20} stroke={1.6} />}
          title="No claims yet"
          hint="Spent your own money on taxis, fuel, meals or supplies for work? Claim it back with the receipt."
          {...(policy.data ? { action: { label: 'New claim', onClick: () => setForm({ claim: null }) } } : {})}
        />
      ) : (
        <DataTable
          rows={rows}
          rowKey={(c) => c.id}
          loading={q.loading}
          error={q.error}
          onRowClick={(c) => setOpen(c.id)}
          columns={[
            {
              key: 't',
              header: 'Claim',
              render: (c) => (
                <>
                  <Text size="sm" fw={500}>
                    {c.title}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {c.firstDate && c.lastDate ? span(c.firstDate, c.lastDate) : ''} · {c.items} {c.items === 1 ? 'item' : 'items'}
                  </Text>
                </>
              ),
            },
            {
              key: 'a',
              header: 'Amount',
              align: 'right',
              render: (c) => (
                <>
                  <Text size="sm">{money(c.totalApproved ?? c.totalClaimed)}</Text>
                  {c.totalApproved && c.totalApproved !== c.totalClaimed ? (
                    <Text size="xs" c="dimmed">
                      of {money(c.totalClaimed)}
                    </Text>
                  ) : null}
                </>
              ),
            },
            { key: 's', header: 'Status', render: (c) => <ClaimBadge status={c.status} /> },
            {
              key: 'p',
              header: 'Payment',
              render: (c) => (
                <Text size="sm" c="dimmed">
                  {c.payoutStatus
                    ? payoutLabel({ status: c.payoutStatus, bsYear: c.payBsYear, bsMonth: c.payBsMonth, code: 'EXP_REIMB' })
                    : c.paymentMethod === 'cash'
                      ? 'Paid outside payroll'
                      : '—'}
                </Text>
              ),
            },
          ]}
        />
      )}
      <Drawer
        opened={!!form}
        onClose={() => setForm(null)}
        position="right"
        size="lg"
        title={<Text fw={600}>{form?.claim ? 'Edit claim' : 'New expense claim'}</Text>}
      >
        {form && policy.data ? (
          <ClaimForm
            policy={policy.data}
            claim={form.claim}
            onDone={(id) => {
              setForm(null);
              void q.reload();
              setOpen(id);
            }}
          />
        ) : null}
      </Drawer>
      <ClaimDrawer
        id={open}
        onClose={() => setOpen(null)}
        onChanged={() => void q.reload()}
        onEdit={(d) => {
          setOpen(null);
          setForm({ claim: d });
        }}
      />
    </Stack>
  );
}
