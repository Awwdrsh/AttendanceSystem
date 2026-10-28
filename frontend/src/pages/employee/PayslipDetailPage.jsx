import { Accordion, Button, Grid, Group, List, Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconPrinter } from '@tabler/icons-react';
import { useParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { Field } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { bsMonthLabel, fmtMoney } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export default function PayslipDetailPage() {
  const { id = '' } = useParams();
  const q = useQuery(() => payApi.payslip(id), [id]);
  if (q.error)
    return (
      <EmptyState
        title="Payslip not available"
        hint={`${q.error} Go back to Payslips and pick another month, or ask HR if this one should be visible.`}
      />
    );
  if (!q.data) return null;
  const { payslip: p, lines } = q.data;
  const group = (type) => lines.filter((l) => l.type === type && Number(l.amount) !== 0);
  const explain = (code) => (p.explain ?? []).find((e) => e.code === code);
  const earnings = group('earning');
  const deductions = group('deduction');
  const employer = group('employer_contribution');
  const employerTotal = employer.reduce((a, l) => a + Number(l.amount), 0);

  const lineRows = (rows) =>
    rows.map((l, i) => {
      const note = explain(l.code)?.text;
      return (
        <Group
          key={l.code}
          justify="space-between"
          align="flex-start"
          wrap="nowrap"
          px="lg"
          py={10}
          style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}
        >
          <div style={{ minWidth: 0 }}>
            <Text size="sm">{l.name_en}</Text>
            {note ? (
              <Text size="xs" c="dimmed">
                {String(note)}
              </Text>
            ) : null}
          </div>
          <Text size="sm" className="num" style={{ flex: 'none' }}>
            {fmtMoney(l.amount, { currency: false })}
          </Text>
        </Group>
      );
    });

  const column = (title, rows, totalLabel, total, empty) => (
    <Paper withBorder h="100%" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <Text fw={600} px="lg" pt="md" pb="xs">
        {title}
      </Text>
      <div style={{ flex: 1 }}>
        {rows.length ? (
          lineRows(rows)
        ) : (
          <Text size="sm" c="dimmed" px="lg" py={10}>
            {empty}
          </Text>
        )}
      </div>
      <Group justify="space-between" px="lg" py="sm" style={{ borderTop: '1px solid var(--hj-line)', background: 'var(--hj-canvas)' }}>
        <Text size="sm" fw={600}>
          {totalLabel}
        </Text>
        <Text size="sm" fw={600} className="num">
          {fmtMoney(total)}
        </Text>
      </Group>
    </Paper>
  );

  return (
    <Stack gap="lg">
      <PageHeader
        title={bsMonthLabel(p.bs_year, p.bs_month)}
        subtitle={`${p.name_en} (${p.emp_code})`}
        back={{ to: '/me/payslips', label: 'Payslips' }}
        actions={
          <Button
            variant="default"
            leftSection={<IconPrinter size={16} stroke={1.6} />}
            onClick={() => window.open(`/api/v1/payroll/payslips/${id}/html`, '_blank', 'noopener')}
          >
            Print payslip
          </Button>
        }
      />

      <Paper withBorder p="lg">
        <Grid gap="lg" align="center">
          <Grid.Col span={{ base: 12, sm: 5 }}>
            <Text size="sm" c="dimmed">
              Net pay
            </Text>
            <Text className="num" fz={36} fw={600} lh={1.1} style={{ letterSpacing: '-0.03em' }}>
              {fmtMoney(p.net)}
            </Text>
            <Text size="sm" c="dimmed" mt={4}>
              Gross {fmtMoney(p.gross)} minus deductions {fmtMoney(p.total_deductions)}
            </Text>
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 7 }}>
            <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="md">
              <Field label="Paid days">
                <span className="num">{Number(p.paid_days)}</span>
              </Field>
              <Field label="Unpaid days">
                <span className="num">{Number(p.lop_days)}</span>
              </Field>
              <Field label="Overtime hours">
                <span className="num">{Number(p.ot_hours)}</span>
              </Field>
              <Field label="Tax deducted">
                <span className="num">{fmtMoney(p.tds)}</span>
              </Field>
            </SimpleGrid>
          </Grid.Col>
        </Grid>
      </Paper>

      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        {column('Earnings', earnings, 'Gross pay', p.gross, 'No earnings this month.')}
        {column('Deductions', deductions, 'Total deductions', p.total_deductions, 'No deductions this month.')}
      </SimpleGrid>

      {employer.length ? (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          <Group justify="space-between" px="lg" pt="md" pb="xs" wrap="wrap">
            <div>
              <Text fw={600}>Paid by your employer</Text>
              <Text size="xs" c="dimmed">
                Contributions on top of your salary. Not deducted from your pay.
              </Text>
            </div>
            <Text size="sm" fw={600} className="num">
              {fmtMoney(employerTotal)}
            </Text>
          </Group>
          {lineRows(employer)}
        </Paper>
      ) : null}

      <Paper withBorder p="lg">
        <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="md">
          <Field label="Taxable income">
            <span className="num">{fmtMoney(p.taxable)}</span>
          </Field>
          <Field label="Gross pay, year to date">
            <span className="num">{fmtMoney(p.ytd?.gross ?? '0')}</span>
          </Field>
          {p.ytd?.tds !== undefined ? (
            <Field label="Tax, year to date">
              <span className="num">{fmtMoney(p.ytd.tds)}</span>
            </Field>
          ) : null}
          {p.ytd?.net !== undefined ? (
            <Field label="Net pay, year to date">
              <span className="num">{fmtMoney(p.ytd.net)}</span>
            </Field>
          ) : null}
        </SimpleGrid>
      </Paper>

      {(p.explain ?? []).length ? (
        <Accordion variant="contained" radius="lg">
          <Accordion.Item value="explain">
            <Accordion.Control>
              <Text size="sm" fw={500}>
                How this pay was calculated
              </Text>
            </Accordion.Control>
            <Accordion.Panel>
              <List size="sm" spacing={4}>
                {(p.explain ?? []).map((e, i) => (
                  <List.Item key={i}>{String(e.text ?? e.line ?? JSON.stringify(e))}</List.Item>
                ))}
              </List>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      ) : null}
      <Text size="sm" c="dimmed">
        Question about this payslip? Contact HR. Every line is backed by a rule or a record they can show you.
      </Text>
    </Stack>
  );
}
