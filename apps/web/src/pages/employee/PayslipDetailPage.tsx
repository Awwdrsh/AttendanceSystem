import { Accordion, Button, Group, Paper, SimpleGrid, Stack, Table, Text, Title } from '@mantine/core';
import { useParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { bsMonthLabel, fmtMoney, fmtNum } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export default function PayslipDetailPage() {
  const { id = '' } = useParams();
  const { t, lang, numerals } = useI18n();
  const q = useQuery(() => payApi.payslip(id), [id]);
  if (q.error) return <EmptyState title="Payslip not available" hint={q.error} />;
  if (!q.data) return null;
  const { payslip: p, lines } = q.data;
  const group = (type: string) => lines.filter((l) => l.type === type && Number(l.amount) !== 0);
  const explain = (code: string) => (p.explain ?? []).find((e) => e.code === code);
  const block = (title: string, type: string) => {
    const rows = group(type);
    if (!rows.length) return null;
    return (
      <Paper withBorder p="md" radius="md">
        <Text fw={600} mb="xs">{title}</Text>
        <Table withRowBorders={false}>
          <Table.Tbody>
            {rows.map((l) => <Table.Tr key={l.code}><Table.Td>{lang === 'ne' && l.name_ne ? l.name_ne : l.name_en}{explain(l.code)?.text ? <Text size="xs" c="dimmed">{String(explain(l.code)?.text)}</Text> : null}</Table.Td><Table.Td ta="right" fw={500}>{fmtMoney(l.amount, { numerals, currency: false })}</Table.Td></Table.Tr>)}
          </Table.Tbody>
        </Table>
      </Paper>
    );
  };
  return (
    <Stack>
      <PageHeader title={`${t('payslips')} · ${bsMonthLabel(p.bs_year, p.bs_month, lang, numerals)}`} subtitle={`${p.emp_code} · ${p.name_en}`}
        actions={<Button variant="light" onClick={() => window.open(`/api/v1/payroll/payslips/${id}/html`, '_blank', 'noopener')}>{t('openPrintable')}</Button>} />
      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="sm">
        {[['Paid days', fmtNum(p.paid_days, numerals)], ['LOP days', fmtNum(p.lop_days, numerals)], ['OT hours', fmtNum(p.ot_hours, numerals)], [t('netPay'), fmtMoney(p.net, { numerals })]].map(([k, v]) => <Paper key={k} withBorder p="sm" radius="md"><Text size="xs" c="dimmed">{k}</Text><Text fw={700}>{v}</Text></Paper>)}
      </SimpleGrid>
      <SimpleGrid cols={{ base: 1, md: 3 }} spacing="sm">
        {block(t('grossPay'), 'earning')}
        {block(t('deductions'), 'deduction')}
        {block(t('employerContributions'), 'employer_contribution')}
      </SimpleGrid>
      <Paper withBorder p="md" radius="md">
        <Group justify="space-between"><Title order={3}>{t('netPay')}</Title><Title order={3}>{fmtMoney(p.net, { numerals })}</Title></Group>
        <Text size="xs" c="dimmed">{t('grossPay')} {fmtMoney(p.gross, { numerals })} − {t('deductions')} {fmtMoney(p.total_deductions, { numerals })} · TDS {fmtMoney(p.tds, { numerals })} · YTD gross {fmtMoney(p.ytd?.gross ?? '0', { numerals })}</Text>
      </Paper>
      {(p.explain ?? []).length ? (
        <Accordion variant="separated">
          <Accordion.Item value="explain"><Accordion.Control>{t('explainPay')}</Accordion.Control><Accordion.Panel>
            <Stack gap={4}>{(p.explain ?? []).map((e, i) => <Text key={i} size="sm">• {String(e.text ?? e.line ?? JSON.stringify(e))}</Text>)}</Stack>
          </Accordion.Panel></Accordion.Item>
        </Accordion>
      ) : null}
      <Text size="xs" c="dimmed">{lang === 'ne' ? 'यो तलब विवरणमा प्रश्न छ? मानव संसाधनलाई सम्पर्क गर्नुहोस्।' : 'Question about this payslip? Contact HR; every line links to a rule or record.'}</Text>
    </Stack>
  );
}
